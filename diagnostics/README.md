# Diagnostics

Interactive troubleshooting scripts. These are **not** an automated test suite —
each one talks to a real TV on your network and some need you standing in front
of it. Nothing here is copied into the Docker image.

Run them from the repo root with a Python that has `samsungtvws` installed:

| Script                 | What it does                                                                                                                                            |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `artmode_watch.py`     | Read-only. Samples REST `PowerState` and the raw `get_artmode_status` reply on one reused connection — the exact pair `is_in_art_mode()` decides on. Use it to see why a TV is being skipped, while it is happening. |
| `token_prompt_test.py` | Connects with a deliberately invalid token to study pairing behaviour. **Raises approval prompts on the TV** and may leave an entry in Device Connection Manager to clean up. Your real token file is never written. |

```bash
python diagnostics/artmode_watch.py 192.168.1.100 <token> --seconds 60
python diagnostics/token_prompt_test.py 192.168.1.100
```
