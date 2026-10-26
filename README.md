# Защищённая оверлейная P2P-сеть (Kademlia + TLS 1.3 + туннели)

Курсовая работа «Защищённая оверлейная сеть передачи данных на основе пиринговых протоколов».
Исполнитель: Вихарев Д.А., группа ИСБ-123. Задание — [docs/assignment/](docs/assignment/).

Узел оверлейной сети на Python: собственное кадрирование поверх TCP, долговременная
Ed25519-идентичность (`NodeID = SHA-256(canonical_encode(pk))`), собственная реализация
Kademlia (k-bucket, итеративный lookup с `ALPHA = 3`, `STORE`/`FIND_VALUE`, репликация `R = 3`,
TTL, refresh, republish), межузловые каналы TLS 1.3 со взаимной аутентификацией и привязкой
сертификата к `NodeID`, туннели через 2 ретранслятора с end-to-end TLS 1.3 между отправителем
и получателем, автоматическое перестроение туннеля после отказа ретранслятора.

**Уровень выполнения — упрощённый**: `N = 15`, `K_BUCKET_SIZE = 3`, `ALPHA = 3`, `R = 3`,
2 ретранслятора, bootstrap-схемы `star` и `ring`. Узлы — независимые процессы на одном
компьютере (адрес `127.0.0.1`, у каждого свой порт, ключ, таблица, хранилище и журнал).

## Требования

- Python 3.10 или новее (проверено на 3.10.11, Windows 11);
- пакеты из `requirements.txt`: `cryptography` (Ed25519, X.509), `msgpack` (сериализация),
  `pytest` (тесты), `matplotlib` (графики);
- свободные TCP-порты на `127.0.0.1`: 9101–9115 (узлы), 10101–10115 (управление узлами),
  9700–9711 (контрольный pcap), 9401–9425 и 10401–10425 (демонстрация этапов 1–2).

Docker, права администратора и сетевые драйверы не нужны.

## Основная команда

```bash
python -m pip install -r requirements.txt
python scripts/run_acceptance.py
```

Команда за 2–3 минуты:

1. запускает автотесты (86 тестов: unit, integration, negative);
2. поднимает сеть из 15 процессов-узлов по схеме `star`, ждёт присоединения всех узлов;
3. проверяет невырожденность DHT, выполняет 30 контрольных lookup, `STORE`/`FIND_VALUE`
   с отказами хранителя, отключение промежуточного узла и bootstrap-узла, 40 сообщений через
   туннели, 5 отказов ретранслятора (kill) и 5 зависаний ретранслятора;
4. снимает контрольный pcap и проверяет, что в нём нет открытого текста;
5. сохраняет всё в **новый** каталог `results/acceptance-<дата-время>/`.

Итог — `results/acceptance-<дата-время>/acceptance_report.md`: таблица из 14 приёмочных
проверок (раздел 16 ТЗ) и таблица измерений. Параметры: `--nodes`, `--scheme star|ring`,
`--k`, `--no-tests`, `--base-port`.

## Быстрая проверка (≈1 минута)

```bash
python -m pytest tests -q          # 86 тестов
python scripts/stage12_demo.py     # демонстрация этапов 1–2 -> results/stage12-<время>/
```

## Ручная работа с сетью

```bash
python scripts/stand.py up --nodes 5 --scheme star      # сеть остаётся работать в фоне
python -m overlay shell --state-dir runs/stand/state/node-01
overlay> table
overlay> send <NodeID получателя> привет
python scripts/stand.py down
```

Все команды — в [USERGUIDE.md](USERGUIDE.md).

## Экспериментальная серия

```bash
python scripts/run_experiments.py                       # ≈5 мин: star и ring, по 5 холодных запусков
python scripts/analyze.py results/experiments-<время>   # таблицы и графики из сырых CSV/JSON
```

## Документация

| Документ | Содержание |
|---|---|
| [USERGUIDE.md](USERGUIDE.md) | команды пользователя, консоль узла, конфигурация |
| [ARCHITECTURE.md](ARCHITECTURE.md) | подсистемы, потоки данных, инженерные решения |
| [PROTOCOL.md](PROTOCOL.md) | формат кадра, типы сообщений, схемы payload, lookup, записи, TLS, туннели |
| [docs/stage12_report.md](docs/stage12_report.md) | краткий отчёт этапов 1–2 |
| [docs/report/](docs/report/) | пояснительная записка и презентация |
| [results/](results/) | сырые данные приёмки, демонстрации и экспериментов |

## Структура

```text
overlay/
  protocol/    кадр (24-байтовый заголовок), типы сообщений, MessagePack-схемы payload
  transport/   TCP-сервер и клиент, пул соединений, тайм-ауты, RPC-корреляция по request_id
  identity/    Ed25519-ключ, NodeID, самоподписанный сертификат для TLS
  security/    TLS 1.3 mTLS с привязкой к NodeID (межузловой канал и end-to-end сессия)
  routing/     XOR-метрика, Contact, k-bucket, RoutingTable, проверка LRU перед вытеснением
  rpc/         обработчики PING, FIND_NODE, STORE, FIND_VALUE
  dht/         итеративный lookup, подписанные NodeRecord, хранилище с TTL и защитой от отката
  tunnel/      построение, пересылка, контроль и закрытие туннелей
  app/         текстовые сообщения: разрешение получателя, туннель, E2E TLS, дедупликация
  node/        узел-фасад (join, lookup_node, put_record, ...), управляющий интерфейс
  metrics/     журнал событий (текст + JSONL) и счётчики
  experiment/  тестовый оркестратор процессов, сценарии, статистика, TCP-tap для pcap
configs/       примеры конфигураций узлов (5 узлов star, 15 узлов star и ring)
scripts/       приёмка, эксперименты, анализ, демонстрация, стенд
tests/         автотесты по этапам
test_vectors/  эталонные векторы NodeID и XOR-метрики
```

Закрытые ключи узлов хранятся только в каталогах состояния (`runs/`, `state/`), которые
исключены из репозитория (`.gitignore`).
