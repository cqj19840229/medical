# fetch_drug_ingredient_smiles.py 使用说明

这个脚本用于从 CSV 或 XLSX 文件中读取活性成分列，调用 PubChem 查询 SMILES，并输出带有 `ActiveIngredient_lookup` 和 `SMILES` 两列的新文件。

## 基本命令

```powershell
python C:\medical\github\medical\scripts\fetch_drug_ingredient_smiles.py `
  --input C:\Users\56884\Desktop\FDA1.xlsx `
  --output C:\Users\56884\Desktop\FDA1_smiles2.xlsx `
  --ingredient-column A
```

python C:\medical\github\medical\scripts\canonicalize_smiles_excel.py `
  --input C:\Users\56884\Desktop\test.xlsx `
  --output C:\Users\56884\Desktop\test_canonical.xlsx `
  --smiles-column D

## 参数说明

- `--input`：输入文件路径，支持 `.csv` 和 `.xlsx`
- `--output`：输出文件路径，支持 `.csv` 和 `.xlsx`
- `--ingredient-column`：活性成分列，可以写列名、Excel 列字母或数字列号
- `--sheet`：XLSX 工作表名，不填写时默认使用活动工作表
- `--cache`：PubChem 查询缓存路径，不填写时默认保存到输出文件同目录的 `pubchem_smiles_cache.json`
- `--no-cache`：忽略已有缓存，强制从头重新查询所有成分
- `--limit`：只查询前 N 个唯一活性成分，测试时可用
- `--timeout`：单次 PubChem 请求超时时间，默认 30 秒
- `--retries`：请求失败后的重试次数，默认 3 次
- `--delay`：每次 PubChem 请求之间的间隔秒数，默认 0.2 秒

## 活性成分列写法

按 Excel 列字母：

```powershell
--ingredient-column C
```

按列号，数字从 1 开始：

```powershell
--ingredient-column 3
```

按表头名称：

```powershell
--ingredient-column ActiveIngredient
```

## 指定工作表

如果输入是 XLSX 且需要读取指定工作表：

```powershell
python C:\medical\github\medical\scripts\fetch_drug_ingredient_smiles.py `
  --input C:\medical\github\medical\split_smile\xy.xlsx `
  --output C:\medical\github\medical\split_smile\xy_smiles1.xlsx `
  --ingredient-column C `
  --sheet 拆分后活性成分表
```

## 输出结果

输出文件会保留原始列，并额外增加：

- `ActiveIngredient_lookup`：实际拆分后用于查询 PubChem 的活性成分
- `SMILES`：PubChem 返回的 SMILES

如果一个单元格中有多个活性成分，并用英文分号 `;` 分隔，脚本会自动拆分成多行输出。

## 缓存说明

脚本会缓存已经查询过的成分，避免重复访问 PubChem。

默认缓存位置：

```text
输出文件同目录\pubchem_smiles_cache.json
```

也可以手动指定：

```powershell
python C:\medical\github\medical\scripts\fetch_drug_ingredient_smiles.py `
  --input C:\medical\github\medical\split_smile\xy.xlsx `
  --output C:\medical\github\medical\split_smile\xy_smiles1.xlsx `
  --ingredient-column C `
  --cache C:\medical\github\medical\split_smile\pubchem_smiles_cache.json
```

如果需要忽略缓存，强制从头重新查询：

```powershell
python C:\medical\github\medical\scripts\fetch_drug_ingredient_smiles.py `
  --input C:\Users\56884\Desktop\A.xlsx `
  --output C:\Users\56884\Desktop\A_smiles.xlsx `
  --ingredient-column B `
  --no-cache
```

## 测试运行

只查询前 10 个唯一活性成分：

```powershell
python C:\medical\github\medical\scripts\fetch_drug_ingredient_smiles.py `
  --input C:\medical\github\medical\split_smile\xy.xlsx `
  --output C:\medical\github\medical\split_smile\xy_smiles_test.xlsx `
  --ingredient-column C `
  --limit 10
```
