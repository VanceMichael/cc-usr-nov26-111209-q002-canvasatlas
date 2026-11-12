# 来源契约

使用 IIIF Presentation API 3 的只读 JSON 文档子集。GET /collection 包含循环 Collection 引用；/book 包含内嵌 Canvas 和结构 Range；/annotations/1 在 initial 阶段返回 503，GET /control/phase?value=complete 后可取，value=updated 后 /book 增加第二页。嵌入对象的相对 URI 按承载文档 URL 解析。文档未包含的字段不表示全协议不支持，只表示本项目不处理。

Canvas.id 为页面身份，Manifest.items 保留页序。Range.items 仅表达分组引用，可重叠。AnnotationPage.items 是 annotation；motivation=painting 的 target 指向 Canvas 或其片段。body 为 Image、Choice 或 SpecificResource；Choice.items 为同一目标候选，SpecificResource.source 给出资源，selector 保留裁切语义。语言映射的每个语言值为数组，none 不是默认语言优先级。Image.service 只登记，禁止递归抓图片像素。

演练不要求认证、Image API 请求或画布渲染。可用 /control/phase 切换来源修订与晚到资源。文档引用形成有向图，不承诺是树；HTTP 失败不能证明目标资源不存在。所有资料都来自允许的本地 origin，跨 origin 引用只能登记受限原因。
