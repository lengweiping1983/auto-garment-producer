# auto-garment-producer

自动化成衣生产 skill：从主题图或已批准面料生成商业成衣裁片样品。短入口规则见 `SKILL.md`，详细约束见 `references/`。

## 常用入口

```bash
python3 scripts/端到端自动化.py --theme-image /path/to/theme.png --out /path/to/output --garment-type "T恤" --mode standard --dual-source --reuse-cache
```

`--out /path/to/output` 会作为任务根目录使用，脚本会自动创建/复用 `output/YYYYMMDD_HHMMSS/`，所有业务产物都写入该任务子目录；显式传入时间戳目录时会直接续跑该目录。

## 关键目录

- `scripts/`：端到端流程、prompt 构造、纹理裁剪、裁片渲染和质检。
- `templates/`：内置多尺寸模板、mask、pieces 与固定 garment map。
- `references/`：数据契约、艺术指导、提示词策略、质检和渲染规则。
