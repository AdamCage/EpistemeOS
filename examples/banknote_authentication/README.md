# Banknote Authentication для вычислительного пилота

Lohweg, V. (2012). *Banknote Authentication* [Dataset]. UCI Machine Learning Repository.
DOI 10.24432/C55P57. Данные опубликованы под лицензией CC BY 4.0.
[Карточка UCI](https://archive.ics.uci.edu/dataset/267/banknote%2Bauthentication) · [Лицензия](https://creativecommons.org/licenses/by/4.0/)

## Короткий пилот Codex

Подготовка создаёт гипотезу, черновик позиции автора, конфигурацию, исходный файл и два CSV. Загрузка ограничена закреплённым архивом UCI, проверяются SHA-256 архива и извлечённого текста. Офлайновый вариант принимает этот же текст через `--source <path>` вместо `--fetch-uci`.

```powershell
$pilot = Join-Path $env:TEMP 'episteme-banknote-inputs'
$state = Join-Path $env:TEMP 'episteme-banknote-state'
python examples/banknote_authentication/prepare_pilot.py $pilot --fetch-uci --model gpt-6.2-sol
$draft = python -X utf8 -m episteme research start --root $state --hypothesis "$pilot/hypothesis.txt" --config "$pilot/config.json" --author pilot-author | ConvertFrom-Json
$draft
```

Проверьте границы вопроса, положение автора и два разрешённых запроса к литературе. При необходимости отредактируйте копию `author-reply-template.txt` и передайте её через `research discuss`. Для принятия используйте `draft_revision` последнего статуса.

```powershell
$draft = python -X utf8 -m episteme research discuss $draft.session --root $state --reply "$pilot/author-reply-template.txt" --author pilot-author | ConvertFrom-Json
# После проверки и принятия автором
python -X utf8 -m episteme research run $draft.session --root $state --accept $draft.draft_revision --author pilot-author
python -X utf8 -m episteme research status $draft.session --root $state
python -X utf8 -m episteme research export $draft.session --root $state --out "${pilot}-result"
```

`start` создаёт черновик без запроса к модели. `run --accept` разрешает два заданных запроса к литературе и до 24 вызовов Codex. В бюджете один stage, восемь попыток исполнения, 1800 секунд на исследование и 120 секунд на операцию. В рабочем каталоге пилота нужны доступный пакет EpistemeOS и установленный Codex CLI с действующей авторизацией. Просмотр bundled-каталога командой `codex debug models --bundled` не проверяет доступность модели у сервиса.

### Захват одной строки на пару признаков

Первый пилот остановился до вычислений после замечания тестировщика о зависимости строк с одинаковыми признаками. Вариант `--capture-version 2` до разбивки оставляет первую по исходному порядку строку на каждую точную пару преобразованных признаков. Метки не влияют на выбор строки или split. На закреплённом снимке остаются 1006 групп train и 342 группы holdout. Общих групп нет, из исходных 1372 строк удалены 24 повтора.

```powershell
$groupedPilot = Join-Path $env:TEMP 'episteme-banknote-grouped-inputs'
python examples/banknote_authentication/prepare_pilot.py $groupedPilot --source "$pilot/data_banknote_authentication.txt" --capture-version 2 --model gpt-6.2-sol
```

Используйте новый каталог состояния и новый черновик. Версия 2 закладывает до 20 вызовов Codex для продолжения пилота после четырёх вызовов первого запуска. Интерпретация ограничена разницей точности на фиксированных представителях. Другие зависимости между строками остаются возможными, а новая выборка и независимая научная оценка требуют отдельной работы.

## Подготовка только CSV

Извлеките `data_banknote_authentication.txt` из архива UCI. Запишите SHA-256 исходного файла до подготовки выборки и используйте его в команде. Укажите новый каталог для пары CSV.

```text
python examples/banknote_authentication/prepare_capture.py data_banknote_authentication.txt banknote-capture --sha256 <sha256> --manifest banknote-provenance.json
episteme pack capture tabular_classification_v1 --source banknote-capture --root <state>
```

Преобразование использует variance и skewness. Значения умножаются на 100000 и округляются до целых по правилу half-even. Хеш пары преобразованных признаков назначает весь набор строк с этой парой в один split. Первый байт хеша меньше 64 даёт holdout, остальные группы попадают в train. Метки не участвуют в назначении. JSON фиксирует исходный хеш, преобразование и хеши обоих CSV. Храните его рядом с каталогом захвата.

Набор публичен и уже использовался для классификации. Сравнение двух признаков с простым baseline проверяет работу контроллера на опубликованных данных. До preregistration укажите известную экспозицию к набору. Holdout из того же источника и пересчёт на нём не дают новой выборки.
