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
