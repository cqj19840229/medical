# AGENTS.md

## 固定 Python 环境

本项目固定使用：

` C:\Users\56884\anaconda3\envs\medical`

所有 Python 脚本必须用这个解释器运行，例如：

` C:\Users\56884\anaconda3\envs\medical exhaustive_smiles_fragments_rebuild.py ...`

安装依赖必须用：

` C:\Users\56884\anaconda3\envs\medical -m pip install ...`

禁止使用：
- `python`
- `python3`
- `/usr/bin/python`
- bundled Python
- 重新查找解释器
- 创建新的虚拟环境

除非用户明确要求排查环境，否则不要执行：
- `which python`
- `whereis python`
- `find / -name python`
- `python --version`

## 项目特点

这是 SMILES 穷举拆分项目，依赖 RDKit、mysql-connector-python、pandas、openpyxl 等库。运行前默认认为依赖已经安装在 medical 环境中。