#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯传统图像处理视觉元素提取器（零 LLM、零语义分割）

依赖：仅 Pillow（无 numpy / sklearn / opencv）
技术栈：MedianCut 颜色量化、边缘密度分块分析、局部方差纹理检测、
        连通域 flood-fill、HSL 色彩空间分类

输出：与 visual_elements.json 兼容的结构，供生成设计简报.py 复用
"""

import json
import math
import sys
import argparse
from pathlib import Path
from PIL import Image, ImageFilter
from collections import Counter


def rgb_to_hsl(r, g, b):
    """RGB [0,255] → HSL: h[0,360], s[0,1], l[0,1]"""
    r_, g_, b_ = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r_, g_, b_), min(r_, g_, b_)
    l = (mx + mn) / 2.0
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r_:
        h = (g_ - b_) / d + (6.0 if g_ < b_ else 0.0)
    elif mx == g_:
        h = (b_ - r_) / d + 2.0
    else:
        h = (r_ - g_) / d + 4.0
    h = (h * 60.0) % 360.0
    return h, s, l


def color_distance(c1, c2):
    """简单的 RGB 欧氏距离"""
    return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5


def hex_color(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _quantize_colors(img: Image.Image, max_colors: int = 24) -> list:
    """使用 Pillow MedianCut 提取主色，返回 [(count, (r,g,b)), ...] 按 count 排序"""
    # 缩小以加速，同时保持代表性
    thumb = img.convert("RGB").resize((80, 80), Image.Resampling.LANCZOS)
    quantized = thumb.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette()  # 扁平 [R,G,B, R,G,B, ...]
    colors = quantized.getcolors(maxcolors=max_colors * 2)  # [(count, index), ...]
    if not colors:
        return []
    result = []
    for count, idx in colors:
        rgb = tuple(palette[idx * 3: idx * 3 + 3])
        result.append((count, rgb))
    result.sort(key=lambda x: x[0], reverse=True)
    return result


def extract_palette(img: Image.Image) -> dict:
    """提取并分类色板：primary / secondary / accent / dark"""
    colors = _quantize_colors(img, max_colors=24)
    if not colors:
        return {"primary": [], "secondary": [], "accent": [], "dark": []}

    # 转换为 HSL 并计算特征
    enriched = []
    for count, rgb in colors:
        h, s, l = rgb_to_hsl(*rgb)
        enriched.append({
            "rgb": rgb,
            "hex": hex_color(rgb),
            "count": count,
            "h": h, "s": s, "l": l,
        })

    # 分类规则
    primary, secondary, accent, dark = [], [], [], []
    used = set()

    # 1. Primary: 最亮且数量多（前3个最亮的）
    bright = sorted([c for c in enriched if c["l"] > 0.55],
                    key=lambda c: c["count"], reverse=True)[:4]
    for c in bright:
        primary.append(c)
        used.add(id(c))

    # 2. Dark: 暗色（亮度 < 0.3）
    dark_candidates = [c for c in enriched if c["l"] < 0.3 and id(c) not in used]
    dark_candidates.sort(key=lambda c: c["count"], reverse=True)
    for c in dark_candidates[:3]:
        dark.append(c)
        used.add(id(c))

    # 3. Accent: 高饱和度（>0.45），且与已有颜色差异大
    for c in enriched:
        if id(c) in used:
            continue
        if c["s"] > 0.45:
            # 检查与已有 accent 的颜色距离，避免太接近
            too_close = any(color_distance(c["rgb"], a["rgb"]) < 40 for a in accent)
            if not too_close:
                accent.append(c)
                used.add(id(c))
                if len(accent) >= 3:
                    break

    # 4. Secondary: 剩余的中等亮度颜色
    for c in enriched:
        if id(c) in used:
            continue
        if 0.25 < c["l"] < 0.7:
            secondary.append(c)
            used.add(id(c))
            if len(secondary) >= 4:
                break

    # Fallback：确保每类至少有一些
    all_remaining = [c for c in enriched if id(c) not in used]
    all_remaining.sort(key=lambda c: c["count"], reverse=True)
    for c in all_remaining:
        if not primary:
            primary.append(c)
        elif not secondary:
            secondary.append(c)
        elif not accent:
            accent.append(c)
        elif not dark:
            dark.append(c)
        used.add(id(c))

    def _to_list(items):
        return [{"hex": c["hex"], "rgb": list(c["rgb"]), "hsl": [round(c["h"], 1), round(c["s"], 2), round(c["l"], 2)]} for c in items]

    return {
        "primary": _to_list(primary),
        "secondary": _to_list(secondary),
        "accent": _to_list(accent),
        "dark": _to_list(dark),
    }


def _compute_grid_stats(img: Image.Image, grid: int = 6) -> list:
    """将图分成 grid×grid 块，返回每块的统计特征"""
    w, h = img.size
    cell_w, cell_h = w // grid, h // grid
    gray = img.convert("L")
    rgb = img.convert("RGB")
    edge = gray.filter(ImageFilter.FIND_EDGES)

    blocks = []
    for row in range(grid):
        for col in range(grid):
            box = (col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h)
            cell_rgb = rgb.crop(box)
            cell_edge = edge.crop(box)
            cell_gray = gray.crop(box)

            # 边缘密度（归一化到 0-1）
            edge_data = list(cell_edge.get_flattened_data())
            edge_density = sum(edge_data) / (255.0 * len(edge_data)) if edge_data else 0

            # 局部颜色方差（无 numpy，手写）
            px = list(cell_rgb.get_flattened_data())
            n = len(px)
            if n == 0:
                variance = 0
                mean_rgb = (128, 128, 128)
            else:
                mr = sum(p[0] for p in px) / n
                mg = sum(p[1] for p in px) / n
                mb = sum(p[2] for p in px) / n
                mean_rgb = (int(mr), int(mg), int(mb))
                # 用 RGB 空间平均标准差作为方差代理
                vr = sum((p[0] - mr) ** 2 for p in px) / n
                vg = sum((p[1] - mg) ** 2 for p in px) / n
                vb = sum((p[2] - mb) ** 2 for p in px) / n
                variance = (vr + vg + vb) / 3.0

            # 亮度
            lum_data = list(cell_gray.get_flattened_data())
            mean_lum = sum(lum_data) / len(lum_data) if lum_data else 128

            blocks.append({
                "row": row, "col": col,
                "box": box,
                "edge_density": round(edge_density, 4),
                "color_variance": round(variance, 2),
                "mean_rgb": mean_rgb,
                "mean_lum": round(mean_lum / 255.0, 3),
            })
    return blocks


def detect_dominant_regions(blocks: list, grid: int = 6) -> list:
    """基于边缘密度和颜色方差，找出主体区域（视觉焦点）"""
    if not blocks:
        return []

    # 综合得分 = 边缘密度 * 0.6 + 归一化方差 * 0.4
    max_var = max(b["color_variance"] for b in blocks) or 1.0
    for b in blocks:
        b["score"] = b["edge_density"] * 0.6 + (b["color_variance"] / max_var) * 0.4

    sorted_blocks = sorted(blocks, key=lambda b: b["score"], reverse=True)

    # 选取 top 块，但做空间去重：相邻块合并为同一个主体
    visited = set()
    regions = []
    for b in sorted_blocks:
        key = (b["row"], b["col"])
        if key in visited:
            continue
        if b["score"] < 0.05:  # 太低不认为是主体
            continue
        # flood-fill 收集相邻高分块
        cluster = []
        queue = [key]
        visited.add(key)
        while queue:
            r, c = queue.pop(0)
            blk = next((x for x in blocks if x["row"] == r and x["col"] == c), None)
            if blk:
                cluster.append(blk)
                for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    nr, nc = r + dr, c + dc
                    nkey = (nr, nc)
                    if 0 <= nr < grid and 0 <= nc < grid and nkey not in visited:
                        nblk = next((x for x in blocks if x["row"] == nr and x["col"] == nc), None)
                        if nblk and nblk["score"] >= 0.03:
                            visited.add(nkey)
                            queue.append(nkey)

        if len(cluster) >= 1:
            # 合并区域属性
            avg_rgb = tuple(int(sum(b["mean_rgb"][i] for b in cluster) / len(cluster)) for i in range(3))
            total_score = sum(b["score"] for b in cluster)
            # 计算外接矩形
            min_r = min(b["row"] for b in cluster)
            max_r = max(b["row"] for b in cluster)
            min_c = min(b["col"] for b in cluster)
            max_c = max(b["col"] for b in cluster)
            regions.append({
                "mean_rgb": avg_rgb,
                "hex": hex_color(avg_rgb),
                "score": round(total_score, 3),
                "cell_count": len(cluster),
                "bbox_cells": [min_r, min_c, max_r, max_c],
                "position": _describe_position(min_r, max_r, min_c, max_c, grid),
                "proportion": round(len(cluster) / (grid * grid), 2),
                "_cells": cluster,  # 保留 cell 数据供 geometry 计算使用
            })

    regions.sort(key=lambda r: r["score"], reverse=True)
    return regions[:3]  # 最多3个主体区域


def _describe_position(min_r, max_r, min_c, max_c, grid):
    """将网格位置描述为自然语言"""
    c_center = (min_c + max_c) / 2.0
    r_center = (min_r + max_r) / 2.0
    h_pos = "center"
    if c_center < grid * 0.35:
        h_pos = "left"
    elif c_center > grid * 0.65:
        h_pos = "right"
    v_pos = "middle"
    if r_center < grid * 0.35:
        v_pos = "upper"
    elif r_center > grid * 0.65:
        v_pos = "lower"
    if h_pos == "center" and v_pos == "middle":
        return "center"
    return f"{v_pos}-{h_pos}" if v_pos != "middle" else h_pos


def detect_texture_regions(blocks: list, grid: int = 6) -> dict:
    """基于颜色方差区分纹理区域和纯色区域"""
    if not blocks:
        return {"textured": [], "solid": []}

    variances = [b["color_variance"] for b in blocks]
    median_var = sorted(variances)[len(variances) // 2]
    threshold = max(median_var * 1.5, 500)

    textured = [b for b in blocks if b["color_variance"] > threshold]
    solid = [b for b in blocks if b["color_variance"] <= threshold]

    return {
        "textured_ratio": round(len(textured) / len(blocks), 2),
        "solid_ratio": round(len(solid) / len(blocks), 2),
        "variance_threshold": round(threshold, 1),
        "median_variance": round(median_var, 1),
    }


def infer_style(palette: dict, regions: list, texture_info: dict, img: Image.Image) -> dict:
    """基于统计特征推断风格（零 LLM）"""
    all_colors = (palette.get("primary", []) + palette.get("secondary", []) +
                  palette.get("accent", []) + palette.get("dark", []))
    if not all_colors:
        return {"medium": "unknown", "mood": "unknown", "pattern_density": "unknown"}

    # 计算整体平均饱和度和亮度
    avg_s = sum(c["hsl"][1] for c in all_colors) / len(all_colors)
    avg_l = sum(c["hsl"][2] for c in all_colors) / len(all_colors)

    # 暖色 vs 冷色比例（简化：hue 0-60 和 300-360 为暖，120-240 为冷）
    warm, cool = 0, 0
    for c in all_colors:
        h = c["hsl"][0]
        if h < 60 or h > 300:
            warm += 1
        elif 120 < h < 240:
            cool += 1
    temp = "warm" if warm > cool else "cool" if cool > warm else "neutral"

    # Pattern density
    textured_ratio = texture_info.get("textured_ratio", 0.5)
    if textured_ratio > 0.7:
        density = "dense"
    elif textured_ratio > 0.4:
        density = "medium"
    else:
        density = "sparse"

    # Mood
    if avg_s < 0.2 and avg_l > 0.6:
        mood = "quiet, minimal, airy"
    elif avg_s < 0.3 and avg_l < 0.5:
        mood = "muted, understated, somber"
    elif avg_s > 0.6:
        mood = "vibrant, energetic, bold"
    elif temp == "warm":
        mood = "warm, inviting, soft"
    else:
        mood = "cool, calm, refined"

    # Medium / brush quality（基于边缘复杂度推断）
    edge_scores = [r["score"] for r in regions] if regions else [0]
    avg_edge = sum(edge_scores) / len(edge_scores)
    if avg_edge > 0.3:
        brush = "highly detailed, intricate linework"
    elif avg_edge > 0.15:
        brush = "balanced detail with clear forms"
    else:
        brush = "soft, blurred, painterly"

    # Line style
    if density == "dense" and avg_edge > 0.2:
        line = "complex interlaced lines"
    elif avg_edge > 0.15:
        line = "clear contours with organic flow"
    else:
        line = "soft diffused edges"

    # Overall impression（用中文描述，因为生成设计简报可以处理）
    impressions = []
    if density == "dense":
        impressions.append("图案密集")
    elif density == "sparse":
        impressions.append("留白较多")
    else:
        impressions.append("疏密适中")
    if temp == "warm":
        impressions.append("暖色调")
    elif temp == "cool":
        impressions.append("冷色调")
    if avg_s > 0.5:
        impressions.append("饱和度高")
    elif avg_s < 0.3:
        impressions.append("低饱和")
    if avg_l > 0.6:
        impressions.append("高明度")
    elif avg_l < 0.4:
        impressions.append("低明度")

    return {
        "medium": " watercolor or digital painting" if avg_edge < 0.2 else "illustration or print design",
        "brush_quality": brush,
        "mood": mood,
        "pattern_density": density,
        "line_style": line,
        "overall_impression": "，".join(impressions) if impressions else "综合风格",
        "derived_stats": {
            "avg_saturation": round(avg_s, 3),
            "avg_lightness": round(avg_l, 3),
            "temperature": temp,
            "edge_complexity": round(avg_edge, 3),
        }
    }


def _eigenvalues_2x2(a: float, b: float, c: float) -> tuple[float, float, float, float]:
    """2x2 对称矩阵 [[a, b], [b, c]] 的特征值分解。
    返回 (lambda1, lambda2, vx, vy)，其中 (vx, vy) 是较大特征值对应的特征向量。
    """
    trace = a + c
    det = a * c - b * b
    discriminant = ((a - c) / 2.0) ** 2 + b * b
    if discriminant < 0:
        discriminant = 0
    sqrt_d = discriminant ** 0.5
    l1 = trace / 2.0 + sqrt_d
    l2 = trace / 2.0 - sqrt_d
    # 较大特征值对应的特征向量
    if l1 >= l2:
        lam = l1
    else:
        lam = l2
    # 解 (a - lam) * vx + b * vy = 0
    if abs(b) > 1e-9:
        vx = b
        vy = lam - a
    else:
        vx = 1.0 if abs(a - lam) < abs(c - lam) else 0.0
        vy = 1.0 if abs(c - lam) < abs(a - lam) else 0.0
    # 归一化
    norm = (vx * vx + vy * vy) ** 0.5
    if norm > 1e-9:
        vx /= norm
        vy /= norm
    return l1, l2, vx, vy


def compute_region_geometry(img: Image.Image, region: dict, blocks: list, grid: int) -> dict:
    """基于原始图像像素为 region 计算精确几何属性（PCA 主轴 + 视觉重心）。

    策略：
    1. 将 region 的 grid cells 转换为像素级 bbox
    2. 在 bbox 内采样像素，用与 region mean_rgb 的颜色距离筛选属于该 region 的像素
    3. 计算筛选后像素的： centroid, covariance, PCA 主轴角度
    4. 根据主轴角度判定 orientation
    """
    w, h = img.size
    cell_w, cell_h = w // grid, h // grid
    rgb_img = img.convert("RGB")

    # 从 region 的 cells 构建像素 bbox
    cells = region.get("_cells", [])
    if not cells:
        # fallback：用 bbox_cells
        min_r, min_c, max_r, max_c = region.get("bbox_cells", [0, 0, 0, 0])
        cells = [{"row": r, "col": c} for r in range(min_r, max_r + 1) for c in range(min_c, max_c + 1)]

    min_r = min(c["row"] for c in cells)
    max_r = max(c["row"] for c in cells)
    min_c = min(c["col"] for c in cells)
    max_c = max(c["col"] for c in cells)

    px_min_x = min_c * cell_w
    px_min_y = min_r * cell_h
    px_max_x = (max_c + 1) * cell_w
    px_max_y = (max_r + 1) * cell_h

    # 限制在图像范围内
    px_min_x = max(0, px_min_x)
    px_min_y = max(0, px_min_y)
    px_max_x = min(w, px_max_x)
    px_max_y = min(h, px_max_y)

    # 保存原始像素尺寸（用于返回 geometry）
    original_pixel_w = px_max_x - px_min_x
    original_pixel_h = px_max_y - px_min_y

    # 缩小采样以加速（最大 128x128 采样窗口）
    sample_w = px_max_x - px_min_x
    sample_h = px_max_y - px_min_y
    if sample_w > 128 or sample_h > 128:
        scale = min(128 / sample_w, 128 / sample_h)
        crop = rgb_img.crop((px_min_x, px_min_y, px_max_x, px_max_y))
        crop = crop.resize((max(1, round(sample_w * scale)), max(1, round(sample_h * scale))), Image.Resampling.LANCZOS)
        px_min_x, px_min_y = 0, 0
        px_max_x, px_max_y = crop.size
    else:
        crop = rgb_img.crop((px_min_x, px_min_y, px_max_x, px_max_y))

    region_mean = region.get("mean_rgb", (128, 128, 128))
    color_thresh = 60  # 颜色距离阈值

    # 收集属于该 region 的像素坐标和颜色
    pixels = list(crop.get_flattened_data())
    cx, cy = crop.size
    coords = []
    colors = []
    for y in range(cy):
        for x in range(cx):
            idx = y * cx + x
            r, g, b = pixels[idx]
            d = ((r - region_mean[0]) ** 2 + (g - region_mean[1]) ** 2 + (b - region_mean[2]) ** 2) ** 0.5
            if d < color_thresh:
                coords.append((x, y))
                colors.append((r, g, b))

    if len(coords) < 10:
        # fallback：降低阈值重试
        color_thresh = 90
        coords = []
        colors = []
        for y in range(cy):
            for x in range(cx):
                idx = y * cx + x
                r, g, b = pixels[idx]
                d = ((r - region_mean[0]) ** 2 + (g - region_mean[1]) ** 2 + (b - region_mean[2]) ** 2) ** 0.5
                if d < color_thresh:
                    coords.append((x, y))
                    colors.append((r, g, b))

    n = len(coords)
    if n < 5:
        # 像素太少，回退到 grid-level 估算
        pixel_w = (max_c - min_c + 1) * cell_w
        pixel_h = (max_r - min_r + 1) * cell_h
        return {
            "pixel_width": pixel_w,
            "pixel_height": pixel_h,
            "canvas_ratio": round((pixel_w * pixel_h) / (w * h), 4),
            "aspect_ratio": round(pixel_w / max(1, pixel_h), 3),
            "orientation": "irregular",
            "visual_center": [0.5, 0.5],
            "form_type": "unknown",
            "_source": "grid_fallback",
        }

    # 计算 centroid
    sum_x = sum(c[0] for c in coords)
    sum_y = sum(c[1] for c in coords)
    cx_mean = sum_x / n
    cy_mean = sum_y / n

    # 计算 covariance matrix
    c_xx = sum((c[0] - cx_mean) ** 2 for c in coords) / n
    c_yy = sum((c[1] - cy_mean) ** 2 for c in coords) / n
    c_xy = sum((c[0] - cx_mean) * (c[1] - cy_mean) for c in coords) / n

    # PCA
    l1, l2, vx, vy = _eigenvalues_2x2(c_xx, c_xy, c_yy)
    angle = math.degrees(math.atan2(vy, vx))
    # 归一化到 [-90, 90]
    while angle > 90:
        angle -= 180
    while angle < -90:
        angle += 180

    # 判定 orientation
    abs_angle = abs(angle)
    if abs_angle < 30:
        orientation = "horizontal"
    elif abs_angle > 60:
        orientation = "vertical"
    else:
        orientation = "irregular"

    # 判定 form_type
    aspect_ratio = (px_max_x - px_min_x) / max(1, px_max_y - px_min_y)
    elongation = max(l1, l2) / max(1e-9, min(l1, l2)) if min(l1, l2) > 1e-9 else 1.0
    if elongation > 3.0:
        form_type = "branch" if orientation == "horizontal" else "tall_flower"
    elif elongation > 1.8:
        form_type = "elongated"
    elif elongation < 1.3 and aspect_ratio < 1.3 and aspect_ratio > 0.77:
        form_type = "round_motif"
    else:
        form_type = "scattered"

    # visual_center：密度加权重心（基于亮度对比度）
    # 亮度越偏离背景 = 越可能是主体部分，权重越高
    bg_lum = sum(region_mean) / 3.0
    weighted_x = sum(c[0] * abs(sum(colors[i]) / 3.0 - bg_lum) for i, c in enumerate(coords))
    weighted_y = sum(c[1] * abs(sum(colors[i]) / 3.0 - bg_lum) for i, c in enumerate(coords))
    total_weight = sum(abs(sum(colors[i]) / 3.0 - bg_lum) for i in range(n))
    if total_weight > 1e-9:
        vis_cx = weighted_x / total_weight
        vis_cy = weighted_y / total_weight
    else:
        vis_cx = cx_mean
        vis_cy = cy_mean

    return {
        "pixel_width": original_pixel_w,
        "pixel_height": original_pixel_h,
        "canvas_ratio": round((original_pixel_w * original_pixel_h) / (w * h), 4),
        "aspect_ratio": round(original_pixel_w / max(1, original_pixel_h), 3),
        "orientation": orientation,
        "visual_center": [round(vis_cx / max(1, cx), 3), round(vis_cy / max(1, cy), 3)],
        "form_type": form_type,
        "_pca_angle": round(angle, 2),
        "_pca_elongation": round(elongation, 2),
        "_source": "pixel_pca",
    }


def build_dominant_objects(regions: list, palette: dict) -> list:
    """将检测到的主体区域包装为 dominant_objects 格式"""
    objects = []
    for i, reg in enumerate(regions):
        h, s, l = rgb_to_hsl(*reg["mean_rgb"])
        suggested = "hero_motif" if i == 0 else "accent_texture" if i == 1 else "secondary_texture"
        # 根据尺寸调整建议用途
        if reg["proportion"] > 0.3:
            suggested = "main_texture"
        obj = {
            "name": f"dominant_region_{i + 1}",
            "color": reg["hex"],
            "form": f"region spanning {reg['cell_count']} grid cells",
            "position": reg["position"],
            "proportion": f"{int(reg['proportion'] * 100)}%",
            "suggested_use": suggested,
            "derived_rgb": list(reg["mean_rgb"]),
            "derived_hsl": [round(h, 1), round(s, 2), round(l, 2)],
        }
        # 如果有 geometry 数据则加入
        if "geometry" in reg:
            obj["geometry"] = reg["geometry"]
        objects.append(obj)
    return objects


def build_supporting_elements(blocks: list, texture_info: dict, grid: int = 6) -> list:
    """构建辅助元素列表（纹理区域、背景、装饰框等）"""
    elements = []

    # 背景色：取边缘密度最低、面积最大的连续区域
    solid_blocks = [b for b in blocks if b["color_variance"] <= texture_info.get("variance_threshold", 500)]
    if solid_blocks:
        # 取最大的连通背景区域
        bg_color = tuple(int(sum(b["mean_rgb"][i] for b in solid_blocks) / len(solid_blocks)) for i in range(3))
        elements.append({
            "name": "background_field",
            "type": "background",
            "visual_features": f"dominant background color {hex_color(bg_color)}, low edge density, likely ground for other elements",
            "derived_color": hex_color(bg_color),
        })

    # 纹理区域
    textured_blocks = [b for b in blocks if b["color_variance"] > texture_info.get("variance_threshold", 500)]
    if textured_blocks:
        elements.append({
            "name": "textured_areas",
            "type": "texture",
            "visual_features": f"{len(textured_blocks)} grid cells show high color variance, suggesting repeating or organic texture",
            "coverage_ratio": texture_info.get("textured_ratio", 0),
        })

    # 边缘框架检测：检查最外圈是否有高密度边缘
    border_cells = [b for b in blocks if b["row"] == 0 or b["row"] == grid - 1 or b["col"] == 0 or b["col"] == grid - 1]
    border_edge = sum(b["edge_density"] for b in border_cells) / len(border_cells) if border_cells else 0
    if border_edge > 0.15:
        elements.append({
            "name": "decorative_border",
            "type": "frame",
            "visual_features": "elevated edge density along image borders suggests a decorative frame or ornamental border",
            "border_edge_density": round(border_edge, 3),
        })

    return elements


def _generate_prompts(palette: dict, style: dict, regions: list) -> dict:
    """基于提取的特征，模板化生成英文提示词（fallback 质量）"""
    primaries = palette.get("primary", [])
    accents = palette.get("accent", [])
    darks = palette.get("dark", [])

    def _color_desc(colors):
        if not colors:
            return "neutral"
        seen = set()
        names = []
        for c in colors[:4]:
            r, g, b = c["rgb"]
            # 简化颜色命名
            if r > 200 and g > 200 and b > 200:
                name = "ivory white"
            elif r > 180 and g > 180 and b < 150:
                name = "warm cream"
            elif r > 150 and g < 120 and b < 120:
                name = "soft red"
            elif r < 120 and g > 150 and b < 120:
                name = "sage green"
            elif r < 120 and g < 120 and b > 150:
                name = "soft blue"
            elif r > 150 and g > 150 and b < 120:
                name = "soft yellow"
            elif r < 80 and g < 80 and b < 80:
                name = "deep charcoal"
            elif r < 100 and g < 100 and b > 120:
                name = "indigo navy"
            elif c["hsl"][1] < 0.15:
                name = "soft gray"
            else:
                name = f"hued tone ({c['hex']})"
            if name not in seen:
                seen.add(name)
                names.append(name)
        return ", ".join(names) if names else "neutral"

    temp = style.get("derived_stats", {}).get("temperature", "neutral")
    density = style.get("pattern_density", "medium")
    mood = style.get("mood", "balanced")

    # Main texture
    main_desc = f"seamless tileable textile texture, {_color_desc(primaries)} ground"
    if density in ("dense", "medium"):
        main_desc += f" with subtle scattered {_color_desc(accents)} botanical or organic motif"
    main_desc += f", {mood}, low noise, lots of negative space, no text"

    # Secondary
    sec_desc = f"coordinating seamless tileable texture, soft {_color_desc(primaries[:1] + accents[:1])} pattern on light ground"
    sec_desc += ", same palette, no text"

    # Accent
    if accents:
        acc_desc = f"tiny scattered {_color_desc(accents)} floral or geometric detail on {_color_desc(primaries[:1])}, small scale repeating"
    else:
        acc_desc = f"delicate micro-pattern on {_color_desc(primaries[:1])}, subtle tonal variation"
    acc_desc += ", seamless tileable, no text"

    # Dark
    if darks:
        dark_desc = f"deep {_color_desc(darks)} ground with tiny {_color_desc(primaries[:1])} pin-dot, very subtle"
    else:
        dark_desc = f"dark tonal texture with minimal pattern, calm and grounding"
    dark_desc += ", seamless tileable, no text"

    # Hero motif
    if regions:
        hero_color = hex_color(regions[0]["mean_rgb"]) if regions[0].get("mean_rgb") else _color_desc(accents[:1] if accents else primaries[:1])
        hero_desc = f"isolated foreground {_color_desc(accents[:1] if accents else primaries[:1])} hero motif only, centered subject, transparent PNG cutout with real alpha background, empty transparent pixels around the subject, no background, no garden, no foliage behind subject, no rectangular composition, no full illustration scene, no colored background box, soft clean edges, designed as placement print element"
    else:
        hero_desc = "isolated foreground floral or organic hero motif only, centered subject, transparent PNG cutout with real alpha background, empty transparent pixels around the subject, no background, no scenery, no rectangular composition, no colored background box, soft clean edges"

    return {
        "main": main_desc,
        "secondary": sec_desc,
        "accent": acc_desc,
        "dark": dark_desc,
        "hero_motif": hero_desc,
        "_source": "cv_fallback",
    }


def extract_visual_elements(image_path: str, grid: int = 6) -> dict:
    """主入口：纯传统图像处理提取视觉元素"""
    img = Image.open(image_path)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # 1. 色板
    palette = extract_palette(img)

    # 2. 网格分析
    blocks = _compute_grid_stats(img, grid=grid)

    # 3. 主体区域
    regions = detect_dominant_regions(blocks, grid=grid)

    # 3b. 为每个 region 计算像素级几何属性（PCA 主轴 + 视觉重心）
    for reg in regions:
        reg["geometry"] = compute_region_geometry(img, reg, blocks, grid)

    # 4. 纹理分析
    texture_info = detect_texture_regions(blocks, grid=grid)

    # 5. 风格推断
    style = infer_style(palette, regions, texture_info, img)

    # 6. 构建输出结构
    dominant_objects = build_dominant_objects(regions, palette)
    supporting_elements = build_supporting_elements(blocks, texture_info, grid=grid)
    generated_prompts = _generate_prompts(palette, style, regions)

    return {
        "_extractor": "pure_cv_no_llm",
        "_method_note": "Zero semantic segmentation. Uses MedianCut quantization, edge density grid analysis, local variance texture detection, and HSL color classification.",
        "dominant_objects": dominant_objects,
        "supporting_elements": supporting_elements,
        "palette": palette,
        "style": style,
        "generated_prompts": generated_prompts,
        "technical_metadata": {
            "grid_size": f"{grid}x{grid}",
            "image_size": list(img.size),
            "texture_analysis": texture_info,
            "top_regions": regions,
        }
    }


def main():
    parser = argparse.ArgumentParser(description="纯传统图像处理视觉元素提取（零 LLM / 零语义分割）")
    parser.add_argument("--theme", required=True, help="主题图路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--grid", type=int, default=6, help="分析网格大小（默认6x6）")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[纯CV提取] 分析主题图: {args.theme}")
    result = extract_visual_elements(args.theme, grid=args.grid)

    out_path = out_dir / "visual_elements_cv.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[纯CV提取] 结果已保存: {out_path}")

    # 同时输出简化版设计简报输入
    brief_path = out_dir / "cv_texture_prompts.json"
    brief = {
        "source": "cv_fallback",
        "collection_prompt": result["generated_prompts"],
        "palette_summary": result["palette"],
        "style_summary": result["style"],
    }
    brief_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[纯CV提取] 设计简报输入已保存: {brief_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
