# astrbot_plugin_repo_kbs_sync

一个独立实现的 AstrBot 插件：把同一个 Git 仓库中不同目录或文件映射到不同知识库，并自动递归同步 Markdown/MDX 文档。

## 主要能力

- 一个仓库配置多条“仓库路径 → 知识库”规则。
- 递归扫描规则路径下的文件，默认白名单为 `.md` 和 `.mdx`，后缀不区分大小写。
- MDX 上传前转换为 Markdown：移除 `import`/`export`、MDX 组件标签和 JSX 表达式，同时保留组件内部文本、Markdown、front matter、代码块和行内代码。
- 首次同步全量导入；之后按 Git 提交差异只更新变化文件，并处理删除和重命名。
- 自动创建不存在的知识库，配置 chunk、嵌入批处理、重试次数等参数。
- 管理员手动同步：`/repo_kbs_sync`。
- 定时检查远端提交，支持主人/群通知。
- 只删除插件自己上次成功同步过的文档，不会因为第一次运行就清空目标知识库里的其他文档。

## 配置示例

在 AstrBot 插件配置中填写：

```json
{
  "repository_url": "https://github.com/example/team-docs.git",
  "branch": "main",
  "sync_rules": [
    {
      "path": "产品文档",
      "kb_name": "产品知识库",
      "enabled": true
    },
    {
      "path": "开发手册",
      "kb_name": "开发知识库",
      "enabled": true
    },
    {
      "path": "FAQ.md",
      "kb_name": "客服知识库",
      "enabled": true
    }
  ],
  "allowed_file_types": [".md", ".mdx"],
  "ignore_paths": ["README.md"],
  "preprocess_mdx": true,
  "chunk_size": 512,
  "chunk_overlap": 50,
  "auto_sync_enabled": true,
  "auto_sync_interval_hours": 24
}
```

`sync_rules.path` 是仓库根目录相对路径：填写目录会递归扫描其全部子目录，填写文件则只同步该文件，填写 `.` 表示整个仓库。规则可以重叠；同一个文件匹配多条规则时，会同步到每个对应知识库。

知识库文档名使用仓库相对路径，例如 `产品文档/入门.mdx`。即使内容被预处理为 Markdown，文档名仍保留 `.mdx`，这样源文件改名、删除和同名文件共存时都能稳定追踪；上传给 AstrBot 的 `file_type` 会在 MDX 预处理开启时统一使用 `md`。

## 同步策略

插件在 AstrBot KV 存储中记录仓库地址、分支、远端提交 SHA、配置指纹和每个知识库的托管文档清单。

- 首次同步、仓库/分支变化、路径映射变化、白名单变化、忽略路径变化、MDX 开关变化或分块参数变化：全量校验。
- 只有远端提交变化且配置未变化：使用 `git diff --name-status --find-renames` 计算增量。
- 删除文档时，只删除清单中由本插件上一次成功同步的文档。
- 如果 Git 历史无法计算差异，会自动回退到全量同步。
- 所有知识库按顺序同步；全部目标完成后才写入新的同步状态，单个知识库失败不会伪造成功状态。

仓库地址支持 HTTPS、SSH、`git@host:path` 和 `file://`。运行环境需要安装 `git`，并且 Git 自身能够访问私有仓库（例如已配置 SSH key 或 credential helper）。

## MDX 预处理边界

预处理器是无依赖的安全文本转换器，不执行 JavaScript。它会：

- 删除顶层 `import` 和 `export` 声明；
- 删除大写或带命名空间/点号的 JSX 组件标签，但保留标签内部文本；
- 删除 JSX 表达式和 MDX 注释；
- 保留 front matter、Markdown、HTML 小写标签、围栏代码块和行内代码。

如果 MDX 文档依赖运行时渲染结果（例如通过表达式动态生成整段文字），预处理器无法执行这些逻辑，建议在仓库中提供静态文本或关闭该类动态写法。

## 安装

将本目录放入：

```text
data/plugins/astrbot_plugin_repo_kbs_sync
```

然后在 AstrBot 插件管理中启用并配置。知识库功能需要 AstrBot 已配置可用的 Embedding Provider；目标知识库不存在时，插件会选择当前第一个可用的 Embedding Provider 自动建库，Rerank Provider 为可选。

## 开发与测试

纯逻辑模块不依赖 AstrBot，可以直接运行：

```bash
python3 -m unittest discover -s tests -v
```

在实际 AstrBot 环境中还需要验证知识库上传、嵌入模型和通知平台配置。
