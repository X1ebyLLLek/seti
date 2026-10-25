# Где остановились (2026-10-25, вечер)

Курсовая «Защищённая оверлейная P2P-сеть», **упрощённый уровень** (N=15, k=3, ALPHA=3, R=3,
TLS 1.3 mTLS, 2 ретранслятора, star+ring). Задание — `docs/assignment/`.
Причина выбора упрощённого уровня: продвинутый требует Docker/namespaces/nftables и netem,
а на этой машине (Windows, нет Docker/WSL/Wireshark) их нет.

## Готово
- Код по всем этапам ТЗ: `overlay/` (protocol, transport, identity, routing, rpc, dht, security,
  tunnel, app, node, metrics, experiment). Консоль: `python -m overlay run|shell|ctl`.
- 84 автотеста проходят: `python -m pytest tests -q` (~1,5 мин).
- Скрипты: `scripts/run_acceptance.py` (одна команда приёмки), `scripts/run_experiments.py` (серия),
  `scripts/analyze.py` (таблицы и графики из CSV), `scripts/stage12_demo.py` (демо этапов 1–2),
  `scripts/bench_serialization.py` (результат уже в `docs/serialization_benchmark.json`),
  `scripts/protocol_examples.py` (результат в `docs/protocol_examples.md`), `scripts/gen_test_vectors.py`.
- Пробный приёмочный прогон: 13 из 14 проверок пройдено. Причины единственного провала (38/40 сообщений:
  у seed-узла не было опубликованной записи) и завышенного времени обнаружения зависшего ретранслятора
  исправлены в последнем коммите. **После исправления приёмку ещё не перезапускали.**

## Что делать дальше (по порядку)
1. `python scripts/run_acceptance.py` (~4 мин) → должно быть 14/14. Результат лежит в `results/acceptance-*`.
2. `python scripts/stage12_demo.py` (~1 мин) — скрипт написан, но ещё ни разу не запускался.
3. `python scripts/run_experiments.py` (~15 мин) → `python scripts/analyze.py results/experiments-*`
   (код графиков `analyze.py` ещё не запускался — проверить картинки глазами).
4. Документы: README.md (одна команда), USERGUIDE.md, ARCHITECTURE.md, PROTOCOL.md (вставить
   `docs/protocol_examples.md` и сравнение сериализаций), отчёт этапов 1–2 (3–5 стр.),
   пояснительная записка (.docx, 15 разделов, ≥12 источников), презентация на 10–12 слайдов (.pptx).
   Титульный лист: Вихарев Д.А., ИСБ-123; ФИО преподавателя не известно — оставить поле пустым.
5. GitHub: новый репозиторий (например `X1ebyLLLek/seti-p2p`). Существующий пустой `X1ebyLLLek/seti`
   **не трогать** без вопроса. Автор коммитов в репо: X1ebyLLLek <huap5665@gmail.com> (как в micropki).

## Заметки
- Каталоги `runs/` (состояния узлов с закрытыми ключами) в git не попадают (`.gitignore`), `results/` — попадают.
- Windows: подключение к закрытому порту на loopback отказывает только через ~2 с. Отсюда
  ~2030 мс у lookup с отключённым промежуточным узлом — это нужно объяснить в записке.
- pcap снимается TCP-tap'ом (`overlay/experiment/tap.py`), потому что Npcap здесь нет; структура pcap
  проверена (контрольные суммы, номера последовательности, записи TLS).
- Тест «дублирование TLS-записи через tap» убран; требование «replay в текущей сессии» закрыто
  прикладным тестом `test_app_replay_in_session_is_deduplicated` и тестом повтора из завершённой сессии.
