# v0.1.0-public.1 发布附件说明

日期：2026-10-08。应用版本为 v0.1.0。本次源码、Windows 安装包、离线文档解析模型包已在 [GitHub 预发布下载页面](https://github.com/bbaicao/zhilan/releases/tag/v0.1.0-public.1)公开，18 件附件均已核对服务器 SHA256、大小与上传状态。原始本地附件位于 release-assets 下的 public-20261008 子目录，线上校验清单与发布记录位于 github-20261008 子目录，源码标签为 `v0.1.0-public.1`；旧标签和旧附件保留，不能用于本次发布。

## 附件清单

| 文件名 | 用途 |
| --- | --- |
| `zhilan-source-0.1.0-public-ready-20261008.zip` | 从本次标签所指提交导出的源码，根目录为 `zhilan-v0.1.0-public.1` |
| `zhilan-setup-0.1.0-public-ready-20261008.exe` | Windows x64 安装器，含程序、离线运行时及许可证目录 |
| `zhilan-models-offline-0.1.0-public-ready-20261008.zip` | 文档解析模型包，655762975 字节 |
| `INSTALLATION-TERMS.txt` | 与安装器许可页输入逐字一致的第三方条款 |
| `ATTACHMENTS-zh-CN.md` | 中文附件说明；原本地文件名为“附件说明.md”，上传后使用 GitHub 兼容名称 |
| `RELEASE-NOTES.md` | 标签时的发布准备正文，实际发布状态见线上发布页 |
| `SOURCE-INVENTORY.json` | 源码提交、标签、树及逐件文件长度和 SHA256 |
| `SHA256SUMS.txt` | 按线上实际附件名生成的最终 SHA256 清单，下载后使用此清单核验 |
| `SHA256SUMS-local-preparation.txt` | 保留的原始本地准备清单，其中中文附件名属于上传前名称 |
| `README.md`、`USAGE.md`、`CONFIGURATION.md`、`ARCHITECTURE.md`、`RELEASE.md`、`CHANGELOG.md`、`LICENSE`、`NOTICE`、`ATTACHMENTS.md` | 发布标签时的使用、配置、架构和许可文档快照 |

安装器 SHA256：`5eae7feb0aa7df785b77558a47d4d00137e4c9d045ef40010144187a5d1182f3`。

模型包 SHA256：`7da76f5c4fa8b9920e11f9c915fa7f49d0ef6e195d9d55363e0b3f335bd48595`。

源码 ZIP 使用 `git archive` 从已审计的提交导出；不含 `.git`、真实配置、用户资料、模型权重、缓存、构建产物或安装器。安装包和模型包用于 Release 附件，不加入源码 Git。`SOURCE-INVENTORY.json` 给出最终提交与标签身份。

## 安装与模型导入

先按 `SHA256SUMS.txt` 核对下载的文件。PowerShell 可用：

```powershell
Get-FileHash .\zhilan-setup-0.1.0-public-ready-20261008.exe -Algorithm SHA256
Get-FileHash .\zhilan-models-offline-0.1.0-public-ready-20261008.zip -Algorithm SHA256
```

阅读 `INSTALLATION-TERMS.txt` 后，由本人决定是否接受并运行安装器。安装包未做开发者代码签名，Windows 可能显示 SmartScreen 或未知发布者提示。默认目录为 `D:\Zhilan`，没有 D 盘时为 `C:\Zhilan`，可在向导中修改。最后一级目录名必须为 ASCII，例如 `Zhilan`；父目录可含中文。问答和向量索引仍需在设置中配置聊天及 Embedding 服务。

启动后，在设置的离线文档解析模型面板选择完整模型 ZIP；无需手动解压。模型包包含 13 件模型及配置、11 件许可及来源文档，展开模型总量为 858204914 字节，导入前需留出足够空间。只导入固定清单绑定的模型，许可文档保留在分发 ZIP 中，请同时保留原 ZIP。首次正常导入显示 13 件导入；重复导入校验已有文件并显示 13 件跳过。

静默安装也需本人先阅读并接受第三方条款，才可使用 `安装器.exe /ACCEPTTHIRDPARTYLICENSES=1`。缺少该参数会中止。本轮隔离安装已获本机所有者授权；这不代表任何下载者已接受条款。

## 本版实测

本次安装器完成 **67 条安装判据、11 次原生运行**：未接受条款时静默安装拒绝、两种无效目录拒绝、两个有效目录首次安装、同版覆盖安装、默认卸载。两个有效目录分别为 ASCII 末级路径及中文父目录路径；覆盖安装保留配置、资料、模型，默认卸载保留这些文件并移除程序和注册信息。

两个实际安装目录分别完成真实设置操作六组：原生选择与取消、坏 ZIP 拒绝并重试、导入后台执行后重开面板、首次 13 件导入、重复 13 件跳过。每个目录分别完成文字 PDF、扫描 PDF、图片三份原样本的 **78 条解析判据**。每次安装后，356 件许可证和元数据文件与编译输入逐件绑定；本轮创建的程序进程均已清理。

此前模型兼容后端小集 50 项、许可小集最终 20 项、命令行导入控制 13 项已有通过记录，本次保留其范围；本次前端许可控制 9 项通过。前端重构建的七件输出与冻结成品逐字相同，运行依赖锁和程序源码绑定后复用程序，以新许可目录重新编译安装器。这里没有声称重新执行上述全部旧检查。

未运行全量回归、缺少 WebView2 的全新机器、物理断网、跨版本升级或删除数据卸载。本次解析未配置远程 Embedding，未调用付费模型，不证明回答质量通过。真实模型回答质量仍未达到目标，引用支持及否定语义问题保留；相似缓存与实体增强检索默认关闭。

2026-10-08 npm 官方 registry 审计：运行依赖 0 项；全部依赖仍为 5 个 high 受影响包，根因为 `braces 3.0.3` 的构建依赖链。`postcss-selector-parser` 已更新至 7.1.6、`source-map-js` 至 1.2.2，两项公告已消失；没有证明剩余公告可从桌面界面直接触发。

## 许可与来源边界

项目原创源码采用 Apache-2.0；第三方组件和模型各自遵守原许可。安装器携带许可证目录及原生许可页，收集器通过表示列出的文件、版本、长度与哈希检查通过，不能代替完整法律合规判断。分发前须阅读以下保留事项及实际条款。

九份 vendor 材料中，七份主许可绑定版本对应的不可变源码；`proxy_tools` 绑定发布前源码，但该版本元数据写 MIT、源码和许可写 BSD，原差异仍保留。`cobble` 只有同版 README 的 BSD-2-Clause 声明，未取得主 LICENSE 和主版权原行，随包主正文明确是 prepared 模板；另附内嵌 inflection 的原 MIT 许可。`proxy_tools` 另附取用 Werkzeug 的原 BSD 许可及作者通知。不能把这些材料写成九份完全无差异的上游主许可。

MinerU 4.0.0 的实际全文包含标准 Apache-2.0 之外的附加条件；WebView2 使用 Microsoft 独立运行时条款，需本人阅读。随包 Microsoft Runtime 与原下载记录中的 CDN 重定向、HTTP 状态、长度及 SHA256 已绑定，原始起始请求 URL 没有登记。

模型 ZIP 保留固定来源、六份模型卡、Apache 全文和转换/配置修改说明。五个直接来源的固定版本声明 Apache-2.0，集合内 Formula 配置另有第三方来源。Layout 与 Formula 原配置的独立下载本轮再次超时，身份由固定官方清单和实算哈希绑定，不能声称独立原文逐字核对完成。

更多使用、配置和发布范围见 [README](README.md)、[USAGE](USAGE.md)、[RELEASE](RELEASE.md) 和 [NOTICE](NOTICE)。GitHub 公开预发布已完成；这不表示已取得无保留的法律结论，也不扩大本版实测范围。源码 ZIP 与独立文档附件保留标签时的准备快照，实际线上状态以发布页面及仓库 `main` 文档为准。
