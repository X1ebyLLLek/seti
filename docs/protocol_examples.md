# Примеры кадров (сгенерированы scripts/protocol_examples.py)

## PING (0x01)

Логическое содержимое payload:

```json
{
 "sender": {
  "node_id": "<32 bytes bd8a63bd14947e94…>",
  "identity_algorithm": "ed25519",
  "identity_public_key": "<32 bytes 8a88e3dd7409f195…>",
  "host": "127.0.0.1",
  "port": 9101
 },
 "timestamp_ms": 1792957896766
}
```

Кадр: заголовок 24 байта + payload 177 байт (MessagePack). Заголовок:

```text
01 | 01 | 0000 | 5f1c0e2a9b7d4c3e8a6f1d2b3c4e5f60 | 000000b1
ver  type flags request_id                         payload_length
```

Начало payload (hex):

```text
82 a6 73 65 6e 64 65 72 85 a4 68 6f 73 74 a9 31 32 37 2e 30 2e 30 2e 31 b2 69 64 65 6e 74 69 74 79 5f 61 6c 67 6f 72 69 74 68 6d a7 65 64 32 35 35 31 39 b3 69 64 65 6e 74 69 74 79 5f 70 75 62 …
```

## PONG (0x02)

Логическое содержимое payload:

```json
{
 "responder": {
  "node_id": "<32 bytes 22055d62f7ed4c90…>",
  "identity_algorithm": "ed25519",
  "identity_public_key": "<32 bytes 8139770ea87d175f…>",
  "host": "127.0.0.1",
  "port": 9102
 },
 "ping_timestamp_ms": 1792957896766,
 "responder_timestamp_ms": 1792957896768
}
```

Кадр: заголовок 24 байта + payload 217 байт (MessagePack). Заголовок:

```text
01 | 02 | 0000 | 5f1c0e2a9b7d4c3e8a6f1d2b3c4e5f60 | 000000d9
ver  type flags request_id                         payload_length
```

Начало payload (hex):

```text
83 b1 70 69 6e 67 5f 74 69 6d 65 73 74 61 6d 70 5f 6d 73 cf 00 00 01 a1 74 9e 58 3e a9 72 65 73 70 6f 6e 64 65 72 85 a4 68 6f 73 74 a9 31 32 37 2e 30 2e 30 2e 31 b2 69 64 65 6e 74 69 74 79 5f …
```

## FIND_NODE_REQUEST (0x03)

Логическое содержимое payload:

```json
{
 "sender": {
  "node_id": "<32 bytes bd8a63bd14947e94…>",
  "identity_algorithm": "ed25519",
  "identity_public_key": "<32 bytes 8a88e3dd7409f195…>",
  "host": "127.0.0.1",
  "port": 9101
 },
 "target_node_id": "<32 bytes a7a5a1e4f95f74c1…>"
}
```

Кадр: заголовок 24 байта + payload 204 байт (MessagePack). Заголовок:

```text
01 | 03 | 0000 | 5f1c0e2a9b7d4c3e8a6f1d2b3c4e5f60 | 000000cc
ver  type flags request_id                         payload_length
```

Начало payload (hex):

```text
82 a6 73 65 6e 64 65 72 85 a4 68 6f 73 74 a9 31 32 37 2e 30 2e 30 2e 31 b2 69 64 65 6e 74 69 74 79 5f 61 6c 67 6f 72 69 74 68 6d a7 65 64 32 35 35 31 39 b3 69 64 65 6e 74 69 74 79 5f 70 75 62 …
```

## FIND_NODE_RESPONSE (0x04)

Логическое содержимое payload:

```json
{
 "responder": {
  "node_id": "<32 bytes 22055d62f7ed4c90…>",
  "identity_algorithm": "ed25519",
  "identity_public_key": "<32 bytes 8139770ea87d175f…>",
  "host": "127.0.0.1",
  "port": 9102
 },
 "target_node_id": "<32 bytes a7a5a1e4f95f74c1…>",
 "contacts": [
  {
   "node_id": "<32 bytes eede01b5fafff9fb…>",
   "identity_algorithm": "ed25519",
   "identity_public_key": "<32 bytes ed4928c628d1c2c6…>",
   "host": "127.0.0.1",
   "port": 9103
  }
 ]
}
```

Кадр: заголовок 24 байта + payload 364 байт (MessagePack). Заголовок:

```text
01 | 04 | 0000 | 5f1c0e2a9b7d4c3e8a6f1d2b3c4e5f60 | 0000016c
ver  type flags request_id                         payload_length
```

Начало payload (hex):

```text
83 a8 63 6f 6e 74 61 63 74 73 91 85 a4 68 6f 73 74 a9 31 32 37 2e 30 2e 30 2e 31 b2 69 64 65 6e 74 69 74 79 5f 61 6c 67 6f 72 69 74 68 6d a7 65 64 32 35 35 31 39 b3 69 64 65 6e 74 69 74 79 5f …
```

## ERROR (0x7F)

Логическое содержимое payload:

```json
{
 "code": "FRAME_TOO_LARGE",
 "message": "payload_length 65537 > 65536"
}
```

Кадр: заголовок 24 байта + payload 59 байт (MessagePack). Заголовок:

```text
01 | 7f | 0000 | 5f1c0e2a9b7d4c3e8a6f1d2b3c4e5f60 | 0000003b
ver  type flags request_id                         payload_length
```

Начало payload (hex):

```text
82 a4 63 6f 64 65 af 46 52 41 4d 45 5f 54 4f 4f 5f 4c 41 52 47 45 a7 6d 65 73 73 61 67 65 bc 70 61 79 6c 6f 61 64 5f 6c 65 6e 67 74 68 20 36 35 35 33 37 20 3e 20 36 35 35 33 36
```

