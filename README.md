# Isaac Wiki Bead Patterns

[![tests](https://github.com/tongsi-git/isaac-wiki-bead-patterns/actions/workflows/tests.yml/badge.svg)](https://github.com/tongsi-git/isaac-wiki-bead-patterns/actions/workflows/tests.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

将 [The Binding of Isaac: Rebirth Wiki](https://bindingofisaacrebirth.wiki.gg/)
中的道具、人物、饰品、卡牌与 Boss 图标转换为带色号的拼豆图纸。

每个图标输出一张独立 PNG。图纸中的每个豆格都直接标注
`221色-MARD,DoDo,小舞家、黄豆豆` 色号，并附带行列坐标、颜色数量和总豆数。

> 本项目是个人非商业工具，与 The Binding of Isaac、Nicalis、PlayBeans
> 及各拼豆色卡品牌无关联。

## 功能

- 通过 MediaWiki API 批量发现并下载 Wiki 图标。
- 使用完整 221 色 MARD 色卡进行 Lab 色彩空间最近色匹配。
- 自动裁除透明边缘、缩放、居中并保留透明空格。
- 每个豆格直接写色号，浅色豆使用深色字，深色豆使用浅色字。
- 添加 1–29 行列坐标、每 5 格辅助线、色号用量表和总豆数。
- 支持断点复用已下载原图，也可离线重新生成其他尺寸。
- 可选导出 PlayBeans `.beans` 工程。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/tongsi-git/isaac-wiki-bead-patterns.git
cd isaac-wiki-bead-patterns
python -m pip install -r requirements.txt
python isaac_beads.py self-test
```

生成完整的 29×29 默认图包：

```bash
python isaac_beads.py build --categories core --size 29
```

Windows PowerShell 中也可直接运行相同命令。输出位于
`output/isaac-beads-221/`。

只生成道具和人物，先做小规模验证：

```bash
python isaac_beads.py build \
  --categories collectibles,characters \
  --limit 5 \
  --size 29
```

## 输出

```text
output/isaac-beads-221/
├── png/
│   ├── collectibles/
│   ├── characters/
│   ├── trinkets/
│   ├── cards/
│   └── bosses/
├── originals/
├── inventory.csv
├── manifest.json
├── summary.json
└── README.txt
```

- `png/`：最终图纸，每个 Wiki 图标一张 PNG。
- `originals/`：下载的原始 Wiki 图片，用于离线重建图纸。
- `inventory.csv`：每个图的色号、各颜色豆数、总豆数和来源链接。
- `manifest.json`：完整下载与处理记录。
- `isaac-beads-mard221-29.zip`：压缩包只包含 PNG 和统计文件，不包含第三方原图。

## 常用命令

生成 21×21 的省豆版本：

```bash
python isaac_beads.py build --categories core --size 21
```

使用已经下载的原图离线重建：

```bash
python isaac_beads.py process --size 21
```

额外生成可导入 PlayBeans 的 `.beans` 文件：

```bash
python isaac_beads.py build --beans
```

### 分类

| 参数 | 内容 |
| --- | --- |
| `collectibles` | 道具 |
| `characters` | 人物和变体外观 |
| `trinkets` | 饰品 |
| `cards` | 卡牌与符文 |
| `bosses` | Boss |
| `pills` | 药丸 |
| `core` | 前五类 |
| `all` | 全部分类 |

### 缩放参数

- `--resample lanczos`：默认，适合把 32×32 图标平滑缩放到 29×29。
- `--resample nearest`：保留硬像素边缘。
- `--margin 1`：图案四周预留 1 颗豆。
- `--alpha-threshold 128`：透明度低于该值的像素视为空格。
- `--workers 4`：并发下载数，请保持适中。

## 色卡说明

项目中的 [palettes/mard221.json](palettes/mard221.json) 包含 PlayBeans
`221色-MARD,DoDo,小舞家、黄豆豆` 色卡的 221 个色号与 RGB 参考值。
`H1` 在 PlayBeans 中具有透明特殊语义，因此该色号不会被用于匹配普通像素；
实际白色使用 `H2`。

## 开发与测试

```bash
python -m unittest discover -s tests -v
python isaac_beads.py self-test
```

测试覆盖色卡、颜色匹配、透明处理、单图标 PNG 输出和可选的 `.beans`
加解密。GitHub Actions 会在 Python 3.10 和 3.12 上运行测试。

欢迎提交 Issue 和 Pull Request，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 版权与许可

项目代码和色卡参考数据使用 [MIT License](LICENSE)。

Wiki 图标和游戏美术作品的版权归其各自权利方所有，不随本项目授权。
仓库不提交 Wiki 图片或生成的图包；运行工具下载的图片仅供个人非商业制作使用。
请遵守 Wiki 的许可、署名要求和访问频率限制。

The source code and palette reference data are MIT licensed. Game artwork remains
the property of its respective rights holders and is not included in this
repository.
