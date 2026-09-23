# 第三方组件与数据说明

本仓库**只包含本项目自己编写的代码**（MIT，见 [`LICENSE`](LICENSE)）。

下列内容**不在本仓库内、也不由本项目分发**，需由使用者自行获取，并各自遵守其许可。

## 不随本仓库分发的内容

| 内容 | 说明 |
|---|---|
| **ComfyUI** | 后端引擎，需自行安装。[ComfyUI 仓库](https://github.com/comfyanonymous/ComfyUI)（GPL-3.0） |
| **模型 / LoRA / VAE / CLIP** | 由使用者自行准备，许可各异（Civitai、HuggingFace 等各有条款） |
| **自定义节点** | 由使用者自行安装，许可各异 |
| **生成的图片** | 由使用者本机模型产出，全部留在使用者自己的机器上 |

本项目仅通过 **HTTP** 与本机运行的 ComfyUI 通信，不打包、不修改、不再分发其源码。ComfyUI 的许可由其自身条款约束，**不因本项目采用 MIT 而改变**。

## 中文标签词库

`fetch_tags.py` 会把上游数据下载到使用者本机，**不随本仓库分发**：

- 仓库：[amenorira/danbooru-tags-data-zh](https://github.com/amenorira/danbooru-tags-data-zh)
- 许可：**MIT**（见上游仓库的 `LICENSE`）
- 下载内容：上游 `tags/` 目录下的 5 个 CSV
- 字段：**标签名、分类、别名、中文译名、图片计数、注释**

**下载内容不含任何图片文件**，也不含原始站点的图片内容。

### 关于 Danbooru

标签数据整理自 [Danbooru](https://danbooru.donmai.us/) 的公开标签体系。

需要注意：**Danbooru 站点自身的代码许可，与站点上用户上传的图片、角色形象、艺术家作品的版权，是两回事**。上游仓库声明的是它对**标签翻译数据**的 MIT 许可，这**不构成**对 Danbooru 上任何图片、角色形象或艺术家作品的授权。

本项目**不使用、不下载、不分发**任何 Danbooru 上的图片。

## 前端

`index.html` 是原生 JS 单文件，**无框架、无 CDN、无外部资源引用**，不依赖任何第三方前端库。

## 提示词助手（可选功能）

若启用 `prompt_help`，本工具会把你输入的中文描述发送到**你自己配置的** LLM 端点，用于生成英文提示词。

- 该功能**默认关闭**（`enabled: false`）
- 端点、模型、API key 全部由使用者自行配置
- 端点可以是任何 OpenAI 兼容服务，本项目不绑定任何特定服务商
- 使用该功能时，请自行确认所选服务商的条款与数据处理政策

## 若你要再分发本项目

请保留：

1. 本项目的 `LICENSE`（MIT）与版权声明
2. 本文件（`THIRD_PARTY_NOTICES.md`）

并且**不要**把模型、LoRA、图片、API key 或他人作品一并打包进去。

## 许可信息的核对

本文档的第三方许可信息，**最后核对于 2026-09-23**：

| 项目 | 许可 | 核对方式 |
|---|---|---|
| ComfyUI | GPL-3.0 | `gh api repos/comfyanonymous/ComfyUI --jq .license.spdx_id` |
| danbooru-tags-data-zh | MIT | `gh api repos/amenorira/danbooru-tags-data-zh --jq .license.spdx_id` |

**上游许可可能变更。** 升级依赖、或距上次核对已过去很久时，请重新核对上表 —— 一条命令的事。
