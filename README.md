# auto-garment-producer

自动化成衣生产 skill：从主题图或面料资产生成商业成衣裁片样品。短入口规则见 `SKILL.md`，详细约束见 `references/`。

## 常用入口

```bash
python3 scripts/端到端自动化.py --theme-image /path/to/theme.png --out /path/to/output --garment-type "T恤" --mode standard --reuse-cache
```

`--out /path/to/output` 会作为任务根目录使用，脚本会自动创建/复用 `output/YYYYMMDD_HHMMSS/`，所有业务产物都写入该任务子目录；显式传入时间戳目录时会直接续跑该目录。

没有现成 `texture_set.json` 或 2x2 看板时，流程调用 Neo AI 并行生成 2x2 面料纹理看板和 1 张完整不裁头的透明主图。有 AI 主图时，程序会裁剪该主图主体区域，保留透明留白后竖向切为左右两半，分别生成 `theme_front_left.png` 和 `theme_front_right.png` 作为可选定位资产。2x2 裁出的 4 个纹理全部分别生成 4 套单纹理 9 裁片模板，直接输出在 `variants/<texture_id>/`。每套变体的 `preview.png` 和 `preview_white.jpg` 只使用当前一张图案纹理，不叠加 motif/主图，以便整套模板保持一致；预览图不作为后续 AI/自动复审输入。

## 关键目录

- `scripts/`：端到端流程、prompt 构造、纹理裁剪和裁片渲染。
- `templates/`：内置固定模板、mask、pieces 与固定 garment map。
- `references/`：数据契约、艺术指导、提示词策略和渲染规则。
