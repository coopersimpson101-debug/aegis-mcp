## What does this change?

<!-- One or two sentences. Link the issue if there is one. -->

## How is it tested?

- [ ] `pytest -q` passes
- [ ] new or changed tools are tested against an in-repo simulator
- [ ] tested on real hardware (say which, and how it was made safe: dry-run, props off, ...)

## Safety checklist

- [ ] risk levels are honest; anything that can cause physical harm is `high` or above
- [ ] domain limits are validated before execution
- [ ] safe exits (land / RTL / pause / heater off / outlet off) are still low risk
- [ ] no confirmation code or secret can reach a tool result, resource or the audit log
