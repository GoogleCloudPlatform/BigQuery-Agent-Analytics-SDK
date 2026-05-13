# Latency Report

Output of `./scripts/latency_report.sh --limit 3 --time-period 1h`:

**Project:** my-gcp-project  
**Dataset:** agent_logs.agent_events  
**Location:** us-central1  

```
Fetching traces from my-gcp-project.agent_logs.agent_events...
Found 3 trace(s)
  Stitched hr_calculator: 4 spans from A2A session load_47a21376...
  Stitched policy_agent: 5 spans from A2A session load_84bf65b0...
  Stitched hr_calculator: 3 spans from A2A session load_cc569bfe...

Session: load_47a21376
Time: 04:35:12  Total: 5.2s
──────────────────────────────────────────────────────────────────────
├── knowledge_supervisor > USER_MESSAGE_RECEIVED
├── knowledge_supervisor > INVOCATION_STARTING
└── knowledge_supervisor > INVOCATION_COMPLETED [5.2s]
    ├── knowledge_supervisor > AGENT_STARTING
    └── knowledge_supervisor > AGENT_COMPLETED [5.2s]
        ├── knowledge_supervisor > LLM_REQUEST
        ├── knowledge_supervisor > LLM_RESPONSE [1.7s, ttft=1.7s]
        ├── knowledge_supervisor > TOOL_STARTING (transfer_to_agent)
        ├── knowledge_supervisor > TOOL_COMPLETED (transfer_to_agent) [0ms]
        ├── knowledge_supervisor ──► hr_calculator [A2A]
        ├── hr_calculator > AGENT_STARTING
        └── hr_calculator > AGENT_COMPLETED [3.5s]
            ┄┄┄ remote session (hr_calculator) ┄┄┄
            ├── hr_calculator > LLM_REQUEST [A2A]
            ├── hr_calculator > LLM_RESPONSE [1.2s, ttft=1.2s] [A2A]
            ├── hr_calculator > TOOL_COMPLETED (calculate_pto_details) [45ms] [A2A]
            └── hr_calculator > LLM_RESPONSE [0.8s] [A2A]

Waterfall:
──────────────────────────────────────────────────────────────────────────────────
  knowledge_supervisor > LLM_RESPONSE                    ██████████████ 1.7s
  knowledge_supervisor > TOOL_COMPLETED (transfer_to_agent) █ 0ms
  hr_calculator > AGENT_COMPLETED                                       ██████████████████████████ 3.5s
  hr_calculator > LLM_RESPONSE [A2A]                                    █████████ 1.2s
  hr_calculator > TOOL_COMPLETED (calculate_pto_details) [A2A]                    █ 45ms
  hr_calculator > LLM_RESPONSE #2 [A2A]                                           ██████ 0.8s
  ──────────────────────────────────────────
  0                   2.6s                   5.2s

══════════════════════════════════════════════════════════════════════════════════

Session: load_84bf65b0
Time: 04:36:08  Total: 3.7s
──────────────────────────────────────────────────────────────────────
├── knowledge_supervisor > USER_MESSAGE_RECEIVED
├── knowledge_supervisor > INVOCATION_STARTING
└── knowledge_supervisor > INVOCATION_COMPLETED [3.7s]
    ├── knowledge_supervisor > AGENT_STARTING
    └── knowledge_supervisor > AGENT_COMPLETED [3.7s]
        ├── knowledge_supervisor > LLM_REQUEST
        ├── knowledge_supervisor > LLM_RESPONSE [1.4s, ttft=1.4s]
        ├── knowledge_supervisor > TOOL_STARTING (transfer_to_agent)
        ├── knowledge_supervisor > TOOL_COMPLETED (transfer_to_agent) [0ms]
        ├── knowledge_supervisor ──► policy_agent [A2A]
        ├── policy_agent > AGENT_STARTING
        └── policy_agent > AGENT_COMPLETED [2.2s]
            ┄┄┄ remote session (policy_agent) ┄┄┄
            ├── policy_agent > LLM_REQUEST [A2A]
            ├── policy_agent > LLM_RESPONSE [0.9s, ttft=0.9s] [A2A]
            ├── policy_agent > TOOL_COMPLETED (lookup_company_policy) [120ms] [A2A]
            ├── policy_agent > TOOL_COMPLETED (get_current_date) [5ms] [A2A]
            └── policy_agent > LLM_RESPONSE [0.7s] [A2A]

Waterfall:
──────────────────────────────────────────────────────────────────────────────────
  knowledge_supervisor > LLM_RESPONSE                         ███████████████████ 1.4s
  knowledge_supervisor > TOOL_COMPLETED (transfer_to_agent)   █ 0ms
  policy_agent > AGENT_COMPLETED                                                  ██████████████████████████████ 2.2s
  policy_agent > LLM_RESPONSE [A2A]                                               ████████████ 0.9s
  policy_agent > TOOL_COMPLETED (lookup_company_policy) [A2A]                               ██ 120ms
  policy_agent > TOOL_COMPLETED (get_current_date) [A2A]                                    █ 5ms
  policy_agent > LLM_RESPONSE #2 [A2A]                                                      █████████ 0.7s
  ──────────────────────────────────────────
  0                   1.9s                   3.7s

══════════════════════════════════════════════════════════════════════════════════

Session: load_cc569bfe
Time: 04:37:22  Total: 3.0s
──────────────────────────────────────────────────────────────────────
├── knowledge_supervisor > USER_MESSAGE_RECEIVED
├── knowledge_supervisor > INVOCATION_STARTING
└── knowledge_supervisor > INVOCATION_COMPLETED [3.0s]
    ├── knowledge_supervisor > AGENT_STARTING
    └── knowledge_supervisor > AGENT_COMPLETED [3.0s]
        ├── knowledge_supervisor > LLM_REQUEST
        ├── knowledge_supervisor > LLM_RESPONSE [1.5s, ttft=1.5s]
        ├── knowledge_supervisor > TOOL_STARTING (transfer_to_agent)
        ├── knowledge_supervisor > TOOL_COMPLETED (transfer_to_agent) [0ms]
        ├── knowledge_supervisor ──► hr_calculator [A2A]
        ├── hr_calculator > AGENT_STARTING
        └── hr_calculator > AGENT_COMPLETED [1.5s]
            ┄┄┄ remote session (hr_calculator) ┄┄┄
            ├── hr_calculator > LLM_REQUEST [A2A]
            ├── hr_calculator > LLM_RESPONSE [0.7s, ttft=0.7s] [A2A]
            ├── hr_calculator > TOOL_COMPLETED (get_remaining_working_days) [30ms] [A2A]
            └── hr_calculator > LLM_RESPONSE [0.5s] [A2A]

Waterfall:
──────────────────────────────────────────────────────────────────────────────────
  knowledge_supervisor > LLM_RESPONSE                              ██████████████████████████ 1.5s
  knowledge_supervisor > TOOL_COMPLETED (transfer_to_agent)        █ 0ms
  hr_calculator > AGENT_COMPLETED                                                             ██████████████████████████ 1.5s
  hr_calculator > LLM_RESPONSE [A2A]                                                          ████████████ 0.7s
  hr_calculator > TOOL_COMPLETED (get_remaining_working_days) [A2A]                                      █ 30ms
  hr_calculator > LLM_RESPONSE #2 [A2A]                                                                   █████████ 0.5s
  ──────────────────────────────────────────
  0                   1.5s                   3.0s

══════════════════════════════════════════════════════════════════════════════════

======================================================================
Summary
======================================================================
  Sessions: 3
  Avg:  4.0s
  P50:  3.7s
  P95:  5.2s
  Min:  3.0s
  Max:  5.2s

  Per-agent latency (avg):
    knowledge_supervisor               3.3s  (n=9)
    hr_calculator                      2.1s  (n=6)
    policy_agent                       1.8s  (n=3)
```
