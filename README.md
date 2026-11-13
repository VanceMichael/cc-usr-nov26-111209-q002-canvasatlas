# canvasatlas

数字样册 IIIF Presentation 3 文档查看器、面向授权 Collection 的采集索引与原文档案。

## 现有能力

- **单文档观察**：`inspect` 查看一份源文件的结构；`capture` 发起一次本机 HTTP 请求并保存响应状态、原始字节和摘要；`show` 列出接收记录。原文对象按 SHA-256 共用，独立请求分别记账。错误 HTTP 状态也会保存，原文归档不等于业务接收完成。
- **Collection 采集索引**：`harvest` 从授权 Collection/Manifest 入口沿引用图采集，工作账持久化在 SQLite，可跨进程续行。`status`/`pages` 给出完整度与逐页视图，`chain` 追溯从入口到任一资源的完整引用链与裁切片段，`search` 做素材检索，`stages` 列出每次抓取的实际阶段。

## 采集规则

- **页数**：只由 Canvas 身份与 `Manifest.items` 页序决定。同一 Canvas 被两个 Range 引用不增加页数；Range 是可重叠的阅读分组；painting annotation 的 Choice 是同一目标的多个候选图，整组保留为同页候选。
- **完整度**：工作账中所有引用均为 `done` 才算整册完整。循环引用只登记一次；HTTP 失败标记 `failed` 并在后续运行重排，不证明资源不存在；无法解析的文档标记 `bad`；不在允许 origin 的引用标记 `blocked` 并保留受限原因。某页引用已采完但没有 painting 图时状态为 `no_image`，与“引用未采完”明确区分。
- **修订隔离**：每次采集用 `--job` 标识一个批次。新批次不会与旧批次拼页，旧批次保持可读；新批次即使部分页已收到，也会在 `incomplete_references` 中说明引用还缺在哪。
- **来源边界**：抓取只落在 job 配置的允许 origin 内（默认入口 URL 的 origin），相对标识按所在文档的最终响应地址解析，重定向跨 origin 会被拒绝。图像服务只登记描述，不下载高分辨率像素。
- **语言**：选定显示语言后仍保留语言映射中的全部原文，`none` 不参与默认自然语言优先级。

## 运行

使用 Python 3.11 或更新版本，仅依赖标准库，无安装步骤。全部示例均为本地合成资料，不包含真实个人信息、商业凭据或线上地址。

```sh
python3 -m canvasatlas inspect canvasatlas/fixtures/manifest.json
python3 -m canvasatlas demo
python3 -m unittest discover -s tests -v
python3 -m compileall -q canvasatlas
```

需要手动发请求时运行 `python3 -m canvasatlas.source_server --port 8765`，再在另一终端运行：

```sh
python3 -m canvasatlas harvest http://127.0.0.1:8765/collection --job batch-1
python3 -m canvasatlas status --job batch-1
python3 -m canvasatlas pages --job batch-1 http://127.0.0.1:8765/book --language zh
python3 -m canvasatlas chain --job batch-1 http://127.0.0.1:8765/image/back
python3 -m canvasatlas search --job batch-1 封面
python3 -m canvasatlas stages --job batch-1
```

晚到的 AnnotationPage 可用 `/control/phase?value=complete` 切换后再次运行同一 `harvest --job`，工作账从 SQLite 续行，直到整册变为完整。默认在当前目录创建 `canvasatlas.sqlite3`（工作账）、`canvasatlas-captures.sqlite3` 与 `objects/`（原文归档），可通过 `--db` 和 `--objects` 指定独立位置。

## 模块

`parser.py` 负责单文档观察，`graph.py` 解析 IIIF 引用图（内嵌/外置 AnnotationPage、Choice、SpecificResource、语言映射、service 登记），`ledger.py` 是跨进程续行的引用工作账，`harvester.py` 协调并行采集与状态机，`index.py` 实现页数规则、引用链追溯与素材检索，`archive.py` 保存不可变原文与请求事实，`transport.py` 按配置 origin 限制请求，`source_server.py` 提供来源示例。来源边界及字段含义见 `docs/source-contract.md`。这些入口不会声称一次响应已经构成完整目录或业务批次。

## 容器

Dockerfile 使用 Python 3.11 标准库镜像，不需要应用依赖下载。可用 `docker build -t canvasatlas:local .` 构建，再执行 `docker run --rm --network none canvasatlas:local`。本地 HTTP 与 PTY 演练不需要外部网络，测试及单次演示通常数秒完成。
