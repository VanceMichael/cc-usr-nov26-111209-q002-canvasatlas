# 来源契约

使用 IIIF Presentation API 3 的只读 JSON 文档子集。GET /collection 包含循环 Collection 引用；/book 包含内嵌 Canvas 和结构 Range；/annotations/1 在 initial 阶段返回 503，GET /control/phase?value=complete 后可取，value=updated 后 /book 增加第二页（/canvas/2 为无 painting 内容的空白页）。嵌入对象的相对 URI 按承载文档 URL 解析。文档未包含的字段不表示全协议不支持，只表示本项目不处理。

Canvas.id 为页面身份，Manifest.items 保留页序。Range.items 仅表达分组引用，可重叠。AnnotationPage.items 是 annotation；motivation=painting 的 target 指向 Canvas 或其片段。body 为 Image、Choice 或 SpecificResource；Choice.items 为同一目标候选，SpecificResource.source 给出资源，selector 保留裁切语义。语言映射的每个语言值为数组，none 不是默认语言优先级。Image.service 只登记，禁止递归抓图片像素。

采集索引把文档引用视为有向图而不是树：

- Collection 可嵌套并循环引用，循环只登记一次，引用链追溯带循环保护。
- AnnotationPage 可以内嵌（携带自身 items，无需另取）或仅以 id 引用（必须另取）；内嵌完整 Canvas 随承载文档登记为文档版本，不再产生独立抓取。
- 同一 Canvas 身份在 Manifest 页序中只占一页；被多个 Range 引用不增加页数，Range 作为可重叠分组单独登记。
- painting 的 Choice 保留全部候选；SpecificResource 的 source 才是图像标识，selector 描述裁切区域，二者随引用链可追溯。
- 工作账状态 pending/claimed/done/failed/blocked/bad 区分“某页没有图像（no_image）”与“引用未采完（references_pending）”。HTTP 失败可重试，不宣布资源不存在；跨 origin 引用只登记 blocked 原因；无法解析的文档标记 bad。
- 每个 --job 是一个修订批次，批次之间页序与候选图互不拼接；旧批次保持可读，新批次在引用未齐时报告具体缺口。工作账持久化在 SQLite，claimed 由下一轮运行回收，支持跨进程续行与并行工作者原子领取。
- 相对标识按所在文档的最终响应地址（含重定向后的地址）解析；所有抓取受 job 的允许 origin 清单限制，重定向落到清单外同样拒绝。

演练不要求认证、Image API 请求或画布渲染。可用 /control/phase 切换来源修订与晚到资源。/linked/book 引用一个其他 origin 的 Manifest（应 blocked）与一个字段不合法的 /broken/doc（应 bad）；/embedded/book 的 Canvas 内嵌带 annotation 的 AnnotationPage（无需另取）。所有资料都来自允许的本地 origin，跨 origin 引用只能登记受限原因。
