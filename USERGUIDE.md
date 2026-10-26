# Руководство пользователя

Все команды выполняются из корня репозитория. Перед первым запуском:
`python -m pip install -r requirements.txt`.

## 1. Стенд из нескольких узлов

Самый простой способ получить работающую сеть — `scripts/stand.py`. Он запускает узлы как
независимые фоновые процессы и сам раздаёт им bootstrap-адреса по выбранной схеме.

| Команда | Что делает |
|---|---|
| `python scripts/stand.py up --nodes 5 --scheme star` | запустить сеть (`--scheme star\|ring\|tree\|multi-seed`, `--k 3`, `--security tls\|plain`) |
| `python scripts/stand.py status` | NodeID, порт, число контактов и записей DHT у каждого узла |
| `python scripts/stand.py stop 3` | штатно остановить узел 3 |
| `python scripts/stand.py kill 3` | аварийно завершить узел 3 (соединения не закрываются штатно) |
| `python scripts/stand.py start 3` | снова запустить узел 3; ключ сохранён, поэтому NodeID тот же |
| `python scripts/stand.py down` | остановить все узлы |

Состояние узлов лежит в `runs/stand/state/node-XX/`. Повторный `up` использует те же ключи.

## 2. Консоль узла

```bash
python -m overlay shell --state-dir runs/stand/state/node-01
```

| Команда | Действие |
|---|---|
| `info` | состояние узла: NodeID, адрес, режим защиты, размер таблицы, записи, соединения, туннели, счётчики |
| `table` | содержимое каждого k-bucket: индекс, заполнение `x/k`, контакты |
| `lookup <NodeID>` | итеративный `FIND_NODE`: итерации, опрошенные узлы, число RPC, длительность, была ли цель в таблице до поиска |
| `get <NodeID>` | `FIND_VALUE` подписанной адресной записи узла |
| `publish` | переопубликовать собственную запись (новый `sequence_number`), показать реплики |
| `storage` | записи DHT, хранящиеся на этом узле |
| `send <NodeID> <текст>` | сообщение через туннель с end-to-end TLS; выводит статус, маршрут и время |
| `tunnels` | активные туннели и история восстановлений после отказов |
| `inbox` | принятые сообщения |
| `history` | история отправки и приёма (`message_id`, статус) |
| `refresh` | принудительное обновление bucket |
| `freeze <секунды>` | (для испытаний) узел перестаёт отвечать на кадры |
| `help`, `quit` | справка, выход (узел продолжает работать) |

NodeID можно задавать префиксом (например, первыми 8 символами), если узел есть в таблице
или истории; иначе нужен полный 64-символьный NodeID — его показывает `stand.py status`
(первые 16 символов) и `info` удалённого узла.

Пример: отказ ретранслятора и восстановление.

```text
overlay> send 8aed36ad... привет
  статус=delivered попыток=1 время=15.5 мс маршрут: 2d2bb9ab -> 35ebe5d1 -> da4a7ed7 -> 8aed36ad
```

В другом окне `python scripts/stand.py kill 2` (номер узла, чей NodeID начинается
с `35ebe5d1`, виден в `stand.py status`), затем снова `tunnels` в консоли: появится строка
`восстановление: ... мс, отказавший=35ebe5d1, новый маршрут ...`, следующий `send` уйдёт
по новому туннелю.

## 3. Одна команда без консоли

```bash
python -m overlay ctl --state-dir runs/stand/state/node-01 info
python -m overlay ctl --state-dir runs/stand/state/node-01 lookup target=<NodeID>
python -m overlay ctl --state-dir runs/stand/state/node-01 send to=<NodeID> text=hello
python -m overlay ctl --state-dir runs/stand/state/node-01 build_tunnel to=<NodeID> relays=2
```

Ответ печатается в JSON. Управляющий интерфейс слушает только `127.0.0.1` и требует токен
из `control.json` в каталоге состояния узла — другой пользователь компьютера без доступа
к этому каталогу узлом управлять не может.

## 4. Запуск отдельного узла вручную

```bash
python -m overlay run --config configs/example-5-star/node-01.json            # seed-узел
python -m overlay run --config configs/example-5-star/node-02.json            # в другом окне
python -m overlay run --config configs/example-5-star/node-02.json --interactive   # сразу с консолью
```

Узел печатает `READY node_id=... listen=... control=...`, после присоединения —
`JOINED ok=True table=<контактов>`. Остановка — Ctrl+C или `ctl ... shutdown`.

Конфигурация собирается в порядке приоритета: значения по умолчанию → JSON-файл (`--config`)
→ переменные окружения с теми же именами → `--set KEY=VALUE`. Пример без файла:

```bash
python -m overlay run --set NODE_STATE_DIR=state/a --set LISTEN_PORT=9201
python -m overlay run --set NODE_STATE_DIR=state/b --set LISTEN_PORT=9202 --set BOOTSTRAP_PEERS=127.0.0.1:9201
```

`BOOTSTRAP_PEERS` — список `host:port` или `<NodeID>@host:port`; во втором случае узел
дополнительно проверяет, что по адресу отвечает именно этот NodeID.

### Основные параметры

| Параметр | По умолчанию | Назначение |
|---|---|---|
| `NODE_STATE_DIR` | `./state/node-01` | каталог ключа, журналов, таблицы и хранилища |
| `LISTEN_HOST`, `LISTEN_PORT` | `127.0.0.1`, `9101` | адрес TCP-сервера |
| `BOOTSTRAP_PEERS` | пусто | начальные контакты (пусто только у seed-узла) |
| `NODE_ID_BITS` | `256` | фиксировано |
| `K_BUCKET_SIZE`, `ALPHA` | `3`, `3` | ёмкость bucket и параллелизм поиска |
| `REPLICATION_FACTOR`, `STORE_MIN_ACKS` | `3`, `2` | реплики записи и порог успешного `STORE` |
| `CONNECT_TIMEOUT_MS`, `READ_TIMEOUT_MS`, `PING_TIMEOUT_MS` | `3000`, `5000`, `5000` | сетевые тайм-ауты |
| `MAX_FRAME_PAYLOAD` | `65536` | предельная длина payload кадра |
| `SECURITY_MODE` | `tls` | `tls` — TLS 1.3 mTLS; `plain` — открытые межузловые каналы (как на этапах 1–2) |
| `RECORD_TTL_S`, `REPUBLISH_INTERVAL_S` | `180`, `90` | срок жизни записи и интервал переопубликования (не более TTL/2) |
| `REFRESH_INTERVAL_S` | `60` | обновление «устаревших» bucket |
| `CONTACT_MAX_FAILS` | `2` | подряд неудачных RPC до удаления контакта |
| `TUNNEL_RELAYS`, `TUNNEL_MAX_HOPS`, `TUNNEL_TTL_S` | `2`, `8`, `600` | ретрансляторы, предел хопов, срок жизни туннеля |
| `TUNNEL_PROBE_INTERVAL_MS`, `TUNNEL_ACK_TIMEOUT_MS`, `TUNNEL_DEAD_AFTER_MISSES` | `1000`, `2000`, `2` | контроль туннеля: зонды, тайм-аут подтверждения, порог `DEAD` |
| `MESSAGE_MAX_BYTES`, `MESSAGE_ACK_TIMEOUT_MS`, `MESSAGE_MAX_ATTEMPTS` | `16384`, `6000`, `4` | ограничения прикладных сообщений |
| `LOG_LEVEL` | `INFO` | `DEBUG` добавляет каждый кадр в журнал |
| `SEED` | `0` | seed выбора ретрансляторов (0 — системный ГСЧ) |

Полный список — `overlay/config.py`. Неизвестное имя параметра — ошибка запуска.

## 5. Что лежит в каталоге состояния узла

| Файл | Содержимое |
|---|---|
| `identity_ed25519.pem` | закрытый ключ (PKCS#8); только здесь, по сети не передаётся |
| `tls_cert.pem` | самоподписанный сертификат с тем же Ed25519-ключом |
| `effective_config.json` | итоговая конфигурация запуска |
| `logs/node.log`, `logs/events.jsonl` | журнал событий: текстовый и машинно-читаемый |
| `bootstrap.json` | результат присоединения: PING bootstrap-узлу, self-lookup, размер таблицы |
| `routing_table.json` | выгрузка k-bucket |
| `lookups.jsonl` | каждый lookup: итерации, опрошенные узлы, RPC, тайм-ауты, длительность |
| `storage.json`, `record_seq.json` | хранимые реплики и последний номер собственной записи |
| `messages.jsonl` | история сообщений |
| `control.json` | порт и токен управляющего интерфейса |

## 6. Сценарии испытаний

| Команда | Время | Результат |
|---|---|---|
| `python -m pytest tests -q` | ≈1 мин | 86 тестов |
| `python scripts/stage12_demo.py` | ≈1 мин | `results/stage12-<время>/` — демонстрация этапов 1–2 |
| `python scripts/run_acceptance.py` | 2–3 мин | `results/acceptance-<время>/` — 14 приёмочных проверок |
| `python scripts/run_experiments.py` | ≈5 мин | `results/experiments-<время>/` — серия для записки |
| `python scripts/analyze.py <каталог серии>` | секунды | `<каталог серии>/analysis/`: `tables.md`, `stats.csv`, `figures/*.png` |
| `python scripts/bench_serialization.py` | секунды | сравнение JSON / MessagePack / CBOR -> `docs/serialization_benchmark.json` |
| `python scripts/make_configs.py` | секунды | пересоздать примеры в `configs/` |
| `python docs/report/build_report.py` | секунды | пояснительная записка `docs/report/Пояснительная_записка.docx` из сохранённых результатов |
| `python docs/report/build_slides.py` | секунды | презентация `docs/report/Презентация.pptx` (текст доклада — в заметках к слайдам) |

Все сценарии создают новый каталог результатов и не меняют исходный код и конфигурации.
