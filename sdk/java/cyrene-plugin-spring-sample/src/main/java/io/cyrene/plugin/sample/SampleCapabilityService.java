// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 SampleCapabilityService.java                                    │
// │  Package: io.cyrene.plugin.sample                                   │
// │  Role: Service consuming typed capability clients without Platform  │
// │        or Exchange dependencies (M4 Exit Gate).                     │
// │                                                                     │
// │  模块职责：业务服务层，直接使用强类型 Client 进行能力调用           │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.sample;

import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.typed.AgentClient;
import io.cyrene.plugin.client.typed.MemoryClient;
import io.cyrene.proto.agent.runtime.v1.AgentRunRequest;
import io.cyrene.proto.agent.runtime.v1.AgentRunResponse;
import io.cyrene.proto.agent.runtime.v1.AgentStreamEvent;
import io.cyrene.proto.memory.provider.v1.MemoryItem;
import io.cyrene.proto.memory.provider.v1.StoreMemoryRequest;
import io.cyrene.proto.memory.provider.v1.StoreMemoryResponse;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

@Service
public class SampleCapabilityService {

    private final AgentClient agentClient;
    private final MemoryClient memoryClient;

    public SampleCapabilityService(AgentClient agentClient, MemoryClient memoryClient) {
        this.agentClient = agentClient;
        this.memoryClient = memoryClient;
    }

    public String executeAgentRun(String bindingId, String prompt) {
        AgentRunRequest request = AgentRunRequest.newBuilder()
            .setPrompt(prompt)
            .build();

        AgentRunResponse response = agentClient.run(bindingId, request, InvocationContext.empty());
        return response.getSuccess().getOutputText();
    }

    public List<String> executeAgentStream(String bindingId, String prompt) {
        AgentRunRequest request = AgentRunRequest.newBuilder()
            .setPrompt(prompt)
            .build();

        Iterator<AgentStreamEvent> iterator = agentClient.runStream(bindingId, request, InvocationContext.empty());
        List<String> deltas = new ArrayList<>();
        while (iterator.hasNext()) {
            deltas.add(iterator.next().getContentDelta().getTextDelta());
        }
        return deltas;
    }

    public String storeMemory(String bindingId, String tenantId, String content) {
        StoreMemoryRequest request = StoreMemoryRequest.newBuilder()
            .setTenantId(tenantId)
            .setItem(MemoryItem.newBuilder().setContent(content).build())
            .build();

        StoreMemoryResponse response = memoryClient.store(bindingId, request, InvocationContext.empty());
        return response.getItemId();
    }
}
