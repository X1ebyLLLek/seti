# Где остановились (2026-10-26)

Курсовая «Защищённая оверлейная P2P-сеть», **упрощённый уровень** (N=15, k=3, ALPHA=3, R=3,
TLS 1.3 mTLS, 2 ретранслятора, star+ring). Задание — `docs/assignment/`.

## Готово (финальная версия кода `5ebcc62`)
- Код всех этапов, 86 тестов (`python -m pytest tests -q`).
- Приёмка `results/acceptance-20261026-053041/` — 14/14; повтор из чистого клона в новом venv — тоже 14/14.
- Серия `results/experiments-20261026-053315/` (+ `analysis/`): 300/300 lookup, 80/80 сообщений, все отказы восстановлены.
- Демо этапов 1–2 `results/stage12-20261026-054134/`.
- Первая серия `results/experiments-20261026-043134/` (код `f0aecfc`) — свидетельство дефекта DHT;
  история исправления в три шага — `results/README.md`.
- README, USERGUIDE, ARCHITECTURE, PROTOCOL, docs/stage12_report.md, scripts/stand.py, configs/.
- Записка `docs/report/Пояснительная_записка.docx` и презентация `docs/report/Презентация.pptx`
  собираются из данных: `python docs/report/build_report.py`, `python docs/report/build_slides.py`.

## Опубликовано
- GitHub: https://github.com/X1ebyLLLek/seti (публичный, ветка `main`; по его выбору — в существующий
  репозиторий; README-заглушка начального коммита заменена через merge, без force-push).

## Осталось
- В титуле записки поле «Принял» и дисциплина не заполнены — данных о них нет.

## Заметки
- Windows: отказ подключения к закрытому порту на loopback ≈ 2 с (4 SYN с шагом ≈ 0,5 с) →
  lookup/FIND_VALUE при отказе узла ≈ 2030 мс; подключение к только что перезапущенному узлу ≈ 510 мс.
- Время сообщения по сессии зависит от загрузки машины: 1,4 / 1,8 / 2,4–3,0 мс в прогонах одного кода.
- Прогноз размера таблицы E[min(3,Bin(14,2^-(j+1)))] = 8,97; наблюдалось 9,1 / 8,8.
- Проверка вида .docx/.pptx — aspose-words/aspose-slides в scratch-venv (LibreOffice нет; пробная
  версия обрезает .docx после ~11 страниц — `build_report.py --preview <раздел> <файл>`).
