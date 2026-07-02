# Medical Workspace

这个仓库是医药相关业务的工作空间。顶层目录按子功能拆分，子功能共享统一的数据目录和模型目录，避免同一份数据或模型在不同子项目里重复保存。

## Directory Layout

- `data/`: 共享数据层，放原始数据、清洗后的数据、跨子功能共用的缓存和中间表。
- `models/`: 共享模型层，放 Hugging Face 缓存、基础模型、本地适配器、向量模型等大模型资产。
- `corefragPredict/`: 核心片段预测相关功能。
- `milvus/`: PDF/说明书/临床指南向量化、检索和 Milvus 导入相关功能。
- `knowledgeGraph/`: 药物知识图谱相关功能。
- `split_smile/`: SMILES、成分和结构拆分相关功能。
- `spl/`: DailyMed SPL 数据导入相关功能。
- `train/`: 通用训练脚本和历史训练流程。
- `project/`: 本地服务/API 原型。
- `doc/`: 跨子功能的设计文档。
- `scripts/`: 跨子功能复用的脚本。
- `logs/`: 运行日志。
- `outputs/`: 临时输出和实验结果。

## Shared Data Rule

子功能需要读取业务数据时，优先从仓库根目录的 `data/` 读取；只有子功能私有、可再生成、且不会被其他模块复用的数据，才放在子功能自己的 `prepared/`、`output/` 或临时目录中。

推荐路径约定：

- 原始数据：`data/raw/` 或当前已有的业务数据目录。
- 清洗结果：`data/processed/`。
- 共享缓存：`data/cache/`。
- 子功能训练集：可以放在子功能目录下的 `prepared/`，但要在 README 中说明来源和生成命令。

## Shared Model Rule

所有模型相关文件统一放到 `models/`，不要在每个子功能目录下重复下载。

推荐路径约定：

- Hugging Face 缓存：`models/huggingface/`
- 本地基础模型：`models/hub/`
- LoRA/QLoRA 适配器和训练产物：`models/adapters/`
- Embedding/rerank 模型：`models/embeddings/`

Windows PowerShell 中运行任务前建议先执行：

```powershell
.\scripts\use_shared_models.ps1
```

这个脚本会把 `HF_HOME`、`TRANSFORMERS_CACHE`、`HF_HUB_CACHE` 和 `MODEL_ROOT` 指向仓库内共享模型目录。
