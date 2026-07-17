# Agent deploy log — bank-auditable trail of what ran, and when

Append-only. Each row is one deploy of a verification agent to Azure AI Foundry. `content_hash` + `git_sha` pin the exact agent definition; `foundry_agent_id` is the live agent that produced evidence from that point until the next row for the same agent.

| deployed_at (UTC) | agent | version | content_hash | git_sha | foundry_agent_id |
|---|---|---|---|---|---|
| 2026-07-17T14:47:25Z | verify_gr_collector | 1.0.0 | sha256:7233b73e79e115a8483dd9df2075c7ebd2254d66ae796a3e4865ab32ae0b1fd0 | a11301c | asst_Z0Ui3DuYkJnzcEWil4fENpKr |
