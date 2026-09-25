# Cyrene Model Provider Contracts

This package is the Python projection of the Plugins-owned
`model.provider.v1` contract. Products use it to encode requests and decode
responses exchanged directly with a resolved Plugin endpoint.

本包是 Plugins 所有的 `model.provider.v1` 契约的 Python 投影。Product 使用它
编码请求、解码响应，并与已解析的 Plugin 端点直接通信。

It contains contract values and codecs only. Provider selection, credentials,
network policy, retries, model lifecycle, and Product state remain outside this
package.

本包只包含契约值与编解码器。提供方选择、凭据、网络策略、重试、模型生命周期
及 Product 状态均不由本包管理。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Cyrene Model Provider Contracts

本 package 是由 Plugins 持有的 model.provider.v1 contract 的 Python 投影。Product 使用它编码 request、解码 response，并与已解析的 Plugin endpoint 直接交换数据。

它只包含 contract value 和 codec。Provider 选择、凭据、网络策略、重试、模型生命周期和 Product 状态都由本 package 之外的组件负责。
