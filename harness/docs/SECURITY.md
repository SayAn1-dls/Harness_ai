# Security

- Workspace paths cannot escape the task root.  
- Shell is policy-gated; obvious destructive commands are blocked.  
- Semantic tools first; `shell()` last.  
- Default GitHub grants exclude `MERGE_PR`.  
- Secrets: do not log env, do not commit `.env`.  
- Sandbox is necessary but not sufficient: **sandbox + policy engine**.  
- Security agent runs when the task text looks like auth/payment/secrets/injection.
