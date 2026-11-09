# AgentWarden — Learning Notes

Offline revision notes. One section is added per completed layer: what was built, how it works, the
key code, the concepts, how to test it, its limitations, and interview questions with short model answers.

## Layer 0: Architecture principles (from the plan)

- **The LLM is an untrusted planner.** It proposes tool calls. A deterministic gateway decides.
  Security that depends on asking an LLM "is this safe?" inherits the LLM's failure modes.
- **One choke point.** Every tool call passes through `Gateway.invoke`. That gives one place to
  enforce, audit, rate-limit and add transports (MCP) later.
- **Defence in depth.** SQL writes are blocked by three independent layers, so one bug isn't enough.
- **Measure, don't assert.** Claims are backed by eval runs stored in MongoDB, and RESULTS.md is generated from them.

### Interview questions
1. *Why not let the model decide whether a tool call is safe?*
   Because the model is the component under attack. Injected text can change its judgement.
   Deterministic rules can't be talked out of a decision.
2. *Why a hand-written loop instead of LangGraph?*
   The control layer (limits, retries, termination, tracing) is the product, so owning it makes every
   behaviour testable and explainable. LangGraph would add checkpointing and graphs we don't need yet.
3. *Why two databases?*
   They sit on different trust boundaries. The agent may read business data. Only the runtime writes
   platform data, and the agent has no tool that can reach its own audit log.
