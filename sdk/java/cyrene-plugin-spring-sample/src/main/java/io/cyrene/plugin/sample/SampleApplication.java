// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 SampleApplication.java                                          │
// │  Package: io.cyrene.plugin.sample                                   │
// │  Role: Clean Spring Boot sample application entrypoint (M4 Exit).   │
// │                                                                     │
// │  模块职责：M4 出口门禁示例应用，演示稳定绑定解析与类型化能力调用      │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.sample;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class SampleApplication {

    public static void main(String[] args) {
        SpringApplication.run(SampleApplication.class, args);
    }
}
