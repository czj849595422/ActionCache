# chat

`chat` wraps Mineflayer chat APIs for public and private messaging.

## Commands

- `chat.say`: send a public chat message visible to all players.
  Params: `message` (required).
- `chat.whisper`: send a private message to a player by username.
  Params: `username` (required), `message` (required).

## Example

```json
{"action": "chat.say", "params": {"message": "hello world"}}
{"action": "chat.whisper", "params": {"username": "bot2", "message": "meet me at base"}}
```

## Notes

- No plugins required. Failures are reported with `invalid_params` / `chat_failed` / `whisper_failed`.
- The skill uses the bot's normal chat channel; whether messages reach players depends on the server's chat settings.
