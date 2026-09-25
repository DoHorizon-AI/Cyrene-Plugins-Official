# ADR 001: Legacy Spring Boot 2 & Java 8 Starter Evaluation (T54)

## Status
Accepted

## Context
Milestone M4 Task T54 states:
> "Add a separate legacy starter only when real consumers prove a Spring Boot 2 or Java 8 requirement. / 只有真实消费者证明需要 Spring Boot 2 或 Java 8 时才增加隔离的 legacy starter。"

Spring Boot 2 reached official End-of-Life (EOL) on November 24, 2023. Java 8 is a legacy runtime lacking modern concurrency (`java.lang.foreign`, virtual threads, records, sealed interfaces) and HTTP/2 Netty/gRPC optimizations.

## Decision
1. The Cyrene Java SDK core baseline is strictly anchored on Java 17+ (JDK 21 LTS / JDK 25 LTS compatible).
2. Separate starters are published for modern production runtimes:
   - `cyrene-plugin-spring-boot-starter` (Spring Boot 3.x baseline)
   - `cyrene-plugin-spring-boot4-starter` (Spring Boot 4.x / Spring 7 baseline)
3. No internal or external consumer has submitted an authorized request for Spring Boot 2 or Java 8.
4. Consistent with T54, no legacy starter will be introduced into the main tree unless an explicit business justification is proven and documented. If required in the future, it shall reside in an isolated legacy module without polluting the clean-room Java client core.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# ADR 001：Legacy Spring Boot 2 与 Java 8 Starter 评估（T54）

## 状态

已接受。

## 背景

里程碑 M4 任务 T54 规定：

> 只有真实消费者证明需要 Spring Boot 2 或 Java 8 时，才增加独立的 legacy starter。

Spring Boot 2 已于 2023 年 11 月 24 日正式结束生命周期（EOL）。Java 8 是缺少现代并发能力的旧版 runtime，包括 java.lang.foreign、virtual thread、record、sealed interface，以及 HTTP/2 Netty/gRPC 优化。

## 决策

1. Cyrene Java SDK core 的基线严格为 Java 17 及以上，并兼容 JDK 21 LTS 和 JDK 25 LTS。
2. 针对现代生产 runtime 发布独立 starter：
   - cyrene-plugin-spring-boot-starter：以 Spring Boot 3.x 为基线。
   - cyrene-plugin-spring-boot4-starter：以 Spring Boot 4.x / Spring 7 为基线。
3. 没有任何内部或外部消费者提交过 Spring Boot 2 或 Java 8 的获授权需求。
4. 按照 T54，除非有明确且经过证明、记录的业务理由，否则不会将 legacy starter 引入主代码树。如果未来确有需要，它必须放在隔离的 legacy module 中，不能污染 clean-room Java client core。
