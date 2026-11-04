# ADR 0026 — контроль исследования только по stages

Дата: 4 ноября 2026. Статус: **принято 4 ноября 2026 координирующим агентом по делегированию пользователя; реализовано и проверено локально.** Пользователь делегировал проектные решения координатору. Решения ниже не переоткрываются. Что делает код и какие отказы покрыты тестами, указано в разделе «Ход реализации»; числа проверок — в [validation.md](../validation.md). Основания: [архитектура](../architecture.md) (§5, §6, §7, §11), [MVP-план](../mvp-plan.md) (M4), ADR [0013](0013-batch-analysis-admission.md), [0016](0016-domain-pack-contract.md), [0018](0018-claim-families-and-review-admission.md), [0020](0020-research-cycle-controller.md), [0025](0025-isolation-role-profiles.md).

## Проблема

Автономность сейчас обрывается раньше науки. [`cycle_step`](../../src/episteme/cycle.py) за вызов пишет не больше одной уже допустимой команды и останавливается с `human_scientific_input` на выборе эксперимента, вердикте, follow-up и confirmatory-интерпретации. Чтобы дойти до claim, оператор ведёт цепочку отдельных команд и обязан назвать planner, analyst и reviewer. [`advance_pack_analysis`](../../src/episteme/analysis_controller.py) после анализа всегда делает `review.assign`. Review, obligations, paper и literature не нужны, чтобы закончить один вычислительный stage.

Если закрытие stage смешать с научным одобрением, exploratory protocol станет confirmatory, а метка `stage-unreviewed` превратится в reviewer. Этого нельзя допустить.

## Решение

Единица управления — stage. Оператор задаёт неизменяемый контракт и читает закрытие. Внутри stage нет отдельных команд на slot, dispatch, анализ и назначение reviewer. Существующие команды, golden histories и `scientific_validity=not_assessed` остаются. `cycle step` остаётся для старого покомандного контура.

```text
stage.open → stage.run → frozen protocol → existing batch roster → pack.analyse → stage.close
```

`stage run` крутит уже существующие механические шаги, пока stage не терминален. Повтор после сбоя продолжает ту же историю и не ставит вторую попытку на `unknown`. Следующий stage сам не открывается. Новый эксперимент, сужение вопроса или другая метрика — новый `stage.open` со ссылкой на закрытый stage, а не тихая правка текущего протокола.

Разработка внутри этого среза — исполнение уже замороженного protocol этого stage, включая код пакета и полный roster. Вызов модели за новым протоколом не входит. Если protocol в контракте нет, `stage run` не сочиняет его и не закрывает stage как выполненный.

### Команды

Новые receipt-команды в allowlist. Payload `pack.analyse` не меняется.

`stage.open` (роль `planner`) замораживает контракт: study из command context, уже существующие question и explanation set, `pack_id`, id уже сделанного `pack.preregister` (событие `pack_binding`), потолок попыток, список критериев. Потолок может быть ниже полного roster: тогда run не запускает попытки и close пишет `stopped_budget`. Optional `predecessor` — id уже закрытого stage той же study. Одновременно открыт не больше одного stage на ту же preregistration.

`stage.close` (роль `planner`) — одна запись после повторного чтения истории. Caller не передаёт disposition. Команда считает критерии и ledger заново на текущих bytes. Пишет disposition, оценку каждого критерия, hashes protocol, settlement и analysis, если они есть. `scientific_validity` остаётся `not_assessed`. Если голова цепочки сдвинулась до записи, expected revision даёт conflict и старое решение не пишется.

### Критерии

Только механические, считаются заново в момент close:

| Критерий | Когда выполнен |
|---|---|
| `analysis_recorded` | Технически completed batch и допущенный `pack.analyse`. Outcome и inference mode копируются из отчёта пакета; stage их не повышает. |
| `primary_metric` | Заранее названная recomputed primary metric сравнивается с порогом. `inconclusive` и отсутствующее число критерий не выполняют. |

Exploratory protocol внутри stage не становится confirmatory. Тот же код — не fresh-seed repetition и не new-data replication; режим берётся из уже записанного roster.

### Disposition закрытия

Отдельно от outcome claim:

| Disposition | Смысл |
|---|---|
| `criteria_met` | Каждый заявленный критерий выполнен на текущих bytes. Это не научное одобрение. |
| `criteria_not_met` | Анализ есть, хотя бы один критерий не выполнен. Отрицательный результат сохраняется. |
| `stopped_budget` | Потолок попыток не покрывает roster; запусков нет. Это не `criteria_met`. |
| `blocked` | Technical failure, `unknown` без completion, дыра в evidence или непройденный gate. Попытка не перезапускается, и пропуск нельзя закрыть как выполнение. |

### Анализ без review

`pack.analyse` по-прежнему требует поле `reviewer_actor`. Stage runner подставляет фиксированную метку `stage-unreviewed`, не вызывает `review.assign` и в close пишет `review=not_requested`. Метка не входит в contributors evidence. Это не научный reviewer и не approval. Разные id actor не называются независимым review: review для этого пути не создаётся.

### Изоляция

Close повторяет факт runner: тот же пользователь ОС, `backend=local_subprocess`, `os_isolation=none`, sandbox нет. Контейнер, четыре checkpoint-файла дыма и `isolation.execute` на этом пути не вызываются.

## Чего на этом пути нет

Не вызываются и не требуются для закрытия: `review.assign`, `review.submit`, veto, obligations, follow-up, tournament, literature, `paper.build`, `episteme package`, контейнер. `cycle step` не удаляется. Golden histories не переписываются: новые события появляются только в новом store.

Не в этом срезе: stage, у которого protocol ещё нет, один раз вызывает pack-experiment proposal; замороженное правило study «не больше N stages», которое само открывает следующий stage. Оба шага не превратят закрытие stage в научное одобрение.

## Ход реализации

`src/episteme/stages.py`: команды `Stage.open` / `Stage.close`, функции `stage_open`, `stage_run`, `stage_status`. `stage_run` продолжает [`advance_batch`](../../src/episteme/batch_controller.py) до settlement; если batch ещё нет и потолок покрывает roster, готовит один search node и `batch.plan` для уже замороженного protocol. Затем хуки и `pack.analyse` без назначения. Close сверяет голову цепочки ещё раз.

CLI: `episteme stage open|run|status`. `run` без отдельного имени «следующего действия».

Тесты на fixture `synthetic_causal_v1`: закрытие без review; `criteria_not_met` при пороге, который exploratory/inconclusive отчёт не выполняет; `blocked` на failed и unknown без повтора и без повышения outcome; потолок меньше roster не запускает попытки; повтор envelope не создаёт второй close; `scientific_validity` остаётся `not_assessed`.
