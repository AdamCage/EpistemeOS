# Состязательный аудит EpistemeOS (коммит da6aa2a), 4 октября 2026

Статус документа: независимая проверка локального прототипа на неизменяемом снимке коммита `da6aa2a367e4b7e1d1f04fab84a26c5916051272` («Add the historical Afterlife DomainPack facade with read-only capture»). Изменения после этого коммита, включая параллельную работу над производительностью проверки receipts, не рассматривались. Отчёт не является научной оценкой, не создаёт событий в журнале и не меняет статусов claims.

Находки ниже датированы снимком `da6aa2a` и не переписываются. Что исправлено позже, указано в разделе 9 «Статус исправлений»; план исправлений — [ADR 0018](decisions/0018-claim-families-and-review-admission.md).

## 1. Область и метод

**Объект.** Пакет `src/episteme` (stdlib-only): SQLite-журнал событий с hash chain, CAS SHA-256, command receipts, protocols, runner и batches, claims, review assignment/delivery/submission, obligations и resolution, paper eligibility, DomainPacks (ADR 0016). Мерило нарушений — контракт `AGENTS.md`.

**Изоляция.** Код получен через `git archive da6aa2a` и распакован во временный каталог; все чтения, PoC и тесты выполнялись там с `PYTHONPATH=<snapshot>\src`. Каждый PoC печатает `episteme.__file__`, подтверждая импорт из снимка. Хранилища `.research` не открывались. В основном дереве создан только этот файл. Встроенный поиск IDE несколько раз вернул результаты из живого дерева вместо снимка; эти результаты отброшены, все цитаты и номера строк перепроверены `rg` внутри снимка.

**Окружение.** Windows 10.0.26200, PowerShell, Python 3.11.15, SQLite 3.53.1. Сеть, LLM/API, платные сервисы и внешние сообщения не использовались; `codex_provider.py` и команды `episteme agent work/advance/reconcile/provider` не запускались. Программы исполнителя в PoC — безвредный локальный Python, пишущий только во временные каталоги аудита.

**Базовая линия.** Полный набор тестов снимка: `python -m unittest discover -s tests` — **544 теста, OK (skipped=4), 838.8 с**. Пропуски — Windows symlink cases.

**Классы атак.** 1) целостность; 2) устаревшее состояние и TOCTOU; 3) поддельные идентичности; 4) утечка контекста; 5) вредоносный DomainPack и программа исполнителя; 6) обход review; 7) раздувание claims; 8) статистическая утечка и cherry-picking.

**Метод.** Для каждой находки — минимальный PoC (скрипт или unittest на снимке) и фактический вывод. Работа шла в три параллельных потока: review/claims/gates/obligations/paper (основной исполнитель), хранилище/CAS/receipts/восстановление и пакеты/раннер/батчи/CLI (два внутренних субагента). Все PoC субагентов, на которые опираются находки, повторно запущены основным исполнителем в отдельной копии каталога; дубликаты объединены. Номера строк относятся к снимку `da6aa2a` и местами отличаются от указанных в задании (например, veto находится в `kernel.py:849-857`, а не 825-829).

**Шкала severity.**

- **critical** — нарушение ключевого инварианта без особых предусловий, не обнаруживаемое существующими проверками, при обратном обещании документации.
- **high** — превращение нерешённого finding, скрытого или отсутствующего evidence в «успех» (`paper_candidate`, paper, claim с ложным происхождением) при реалистичных предусловиях: обычные роли команды и добросовестный reviewer.
- **medium** — обход, требующий более сильного предусловия (in-process код, автор программы или пакета, владелец файлов), либо документированная граница с тривиальной эксплуатацией и заметным эффектом на итоговые артефакты.
- **low** — отказ в обслуживании, ложные тревоги, несоответствия UX/документации без прямого превращения в «успех».

**Предусловия** указаны как: владелец файлов; CLI caller с произвольными actor strings; автор пакета; автор программы исполнителя; reviewer; in-process caller (код в том же Python-процессе с доступом к `Store`). **Классификация:** bug; недокументированный разрыв; документированная by-design граница (с цитатой).

## 2. Сводка

Критических находок нет: ядро последовательно применяет optimistic concurrency, проверку basis в момент записи, replay receipt-backed событий и проверку hash при каждом чтении CAS. Самые серьёзные проблемы — логические, и они сохранятся даже после аутентификации actor:

- veto и открытые obligations привязаны к ID claim, поэтому повторный claim на тех же evidence их обходит, а резолюция, выданная для одного claim, переносится на соседний (A-01, A-02);
- потолок силы claim пакета снимается одним amendment протокола, а legacy-путь `analysis.apply` принимает proposal, которого адаптер не выдавал (A-03, A-04);
- противоречащие попытки под другим протоколом остаются в журнале, но не попадают ни в слепой bundle рецензента, ни в paper (A-05).

Документация в целом честна о caller-declared identity, отсутствии sandbox и уязвимости к владельцу файлов. Однако ряд формулировок в README, architecture.md и ADR 0001/0013/0016 сильнее фактического поведения (раздел 6).

| ID | Severity | Кратко | Предусловия | Классификация |
| --- | --- | --- | --- | --- |
| A-01 | high | Повторный claim на том же протоколе обходит veto и открытые obligations; paper собирается | analyst + planner, честный второй reviewer | bug / недокументированный разрыв |
| A-02 | high | Резолюция obligation для узкого child-claim разблокирует соседний claim с более сильным выводом | analyst; резолюция исходного reviewer; любой второй reviewer | bug |
| A-03 | high | Amendment pack-bound протокола снимает потолок и pack-only допуск: `supports` из неисполненных runs | CLI caller (planner, executor, replicator, analyst) | недокументированный разрыв, противоречит ADR 0016:262 |
| A-04 | high | `analysis.apply` принимает proposal, не вычисленный адаптером, под его ID и digest | analyst | недокументированный разрыв, противоречит ADR 0013:16 |
| A-05 | high | Противоречащие попытки под другим протоколом скрыты от слепого reviewer и paper | planner + executor | недокументированный разрыв, противоречит ADR 0002:27 |
| A-06 | high | Legacy `review`: verdict без assignment, снятие чужого veto подменой ID, paper из demo | CLI caller | документированная известная проблема (ADR 0016:211) |
| A-07 | medium | Повтор seed до «успеха»; paper скрывает метрики failed-попытки | executor / автор программы | недокументированный разрыв |
| A-08 | medium | «Независимый реанализ» копированием digests оригинала | CLI caller (replicator) | документированная граница |
| A-09 | medium | Exploratory → confirmatory через перекодированную копию данных | planner + executor | документированная граница (ADR 0002:15) |
| A-10 | medium | Read side принимает любое событие `review`: самоодобрение через `Store.append` | in-process caller / владелец файлов | частично документированная граница |
| A-11 | medium | Пакет: обход статической проверки, доступ к живому Store и скрытым inputs, подмена потолка в процессе | автор пакета | документированная граница (ADR 0016:61, 247) |
| A-12 | medium | Программа исполнителя: полное окружение, запись вне workspace, перепись хранилища | автор программы исполнителя | документированная граница, последствия не описаны |
| A-13 | medium | Владелец файлов переписывает историю незаметно; `INSERT OR REPLACE` обходит triggers без их удаления | владелец файлов / SQL-доступ | граница документирована; REPLACE — bug против architecture.md:77, ADR 0001:85 |
| A-14 | low | Слепой bundle несовместим с обязательным acknowledgement чужих findings; reviewer блокируется на basis | — | противоречие дизайна |
| A-15 | low | Restore старого снимка: тихий откат и потеря идемпотентности команд | оператор / владелец файлов | недокументированный разрыв |
| A-16 | low | Нейтрализованные triggers переживают reopen, backup и restore | владелец файлов / программа исполнителя | недокументированный разрыв |
| A-17 | low | Graph: ложные `IntegrityError` при конкурентной записи | — | bug |
| A-18 | low | Hash покрывает разобранный JSON, не текст: дубликаты ключей дают разные verdicts для разных читателей | владелец файлов | недокументированный разрыв |
| A-19 | low | `inspect`/`gate` не делают replay-проверку; `Store.receipts()` принимает поддельные receipts | владелец файлов | недокументированный разрыв |
| A-20 | low | Повреждённый CAS-файл нельзя восстановить через `put()`; один orphan блокирует backup | владелец файлов / программа / сбой диска | недокументированный разрыв |
| A-21 | low | Каталог snapshot принимается как `--root` и портится; крах restore оставляет полуопубликованный каталог и полные копии staging | любой CLI caller / сбой | частично документировано |
| A-22 | low | Actor IDs не нормализуются (пробел, гомоглиф); analyst может совпадать с planner/executor | CLI caller | документированная граница с разрывом |
| A-23 | low | «Read-only» `pack describe` и `pack verify` исполняют код пакета | автор пакета | документированная граница с оговоркой |

## 3. Находки

Общий helper `Lab` (приложение A) используют PoC A-01, A-07, A-08, A-10, A-14 и раздела 4. PoC, наследующие тестовые fixtures (A-02, A-04, A-05, A-09), запускались с `PYTHONPATH=<snapshot>\src;<snapshot>;<snapshot>\tests;<work>`.

### 3.1. High

#### A-01. Повторный claim на том же протоколе обходит veto и открытые obligations

- **Severity: high.** Нарушает главный пункт контракта («never turn … unresolved scientific findings into success») без подмены identity: достаточно обычных ролей analyst/planner и добросовестного второго reviewer.
- **Предусловия:** caller с ролями analyst (новый claim) и planner (назначение reviewer) через `CommandService`.
- **Классификация:** bug / недокументированный разрыв. Противоречит README.md:147 («Отрицательное мнение одного reviewer не отменяется одобрением другого или изменением basis»), README.md:176 («veto отрицательного review и блокировка premature paper») и ADR 0009:37 (paper scaffold не допускается «для исходного или successor claim даже после позднего `approve`»). Тест `test_open_source_obligation_blocks_a_reviewed_successor_paper` покрывает только вариант с явной связью `supersedes`; ADR 0013:12 прямо разрешает несколько «bounded кандидатов» на один batch, не обсуждая наследование veto.
- **Где:** `Kernel._next_action`, `src/episteme/kernel.py:823-866`. Obligations собираются только для `resolve_context(...)` и источников `replan_followup` в линии протоколов (827-848), veto — только из reviews с `payload.claim == claim` (849-855). `Kernel._claim` (450-476) позволяет записать второй claim на тех же runs. Политика `blind_initial_review_v1` исключает прежние verdicts (`review_assignment.py:23-32`), поэтому второй reviewer не узнаёт об отклонении. Для pack-bound протоколов вариант не воспроизводится: claims там создаёт только `pack.analyse`, а повтор задачи с тем же отчётом отклоняется.

PoC (`poc_r1_duplicate_claim.py`, полный слепой путь ADR 0011/0012):

```python
lab = Lab("poc-r1-")
claim_a = lab.basic_claim_world()
finding = dict(kind="discriminating_experiment", action="Rule out confounding",
               closure_criterion="A discriminating experiment is reviewed",
               evidence_refs=[claim_a, lab.primary])
lab.cmd("reviewer-R1", "reviewer", "replanning.record_review", claim=claim_a, verdict="reject",
        rationale="Confounded design", findings=[finding], expected_basis=lab.basis(claim_a),
        link_assessments=None)
print("A next_action:", lab.next_action(claim_a)["action"])
claim_b = lab.new_claim()                       # тот же протокол, evidence и текст
basis_b = lab.basis(claim_b)
assigned = lab.cmd("planner-1", "planner", "review.assign", claim=claim_b,
                   reviewer_actor="reviewer-R2", expected_basis=basis_b)
manifest = lab.store.read(assigned["bundle"]).decode()
print("bundle mentions claim A:", claim_a in manifest, "| mentions R1:", "reviewer-R1" in manifest)
lab.cmd("planner-1", "planner", "review.dispatch", assignment=assigned["assignment"],
        provider_id="local-human")
response = lab.store.put(canonical(dict(schema_version=1, assignment=assigned["assignment"],
    bundle=assigned["bundle"], claim=claim_b, basis_hash=basis_b, reviewer_actor="reviewer-R2",
    verdict="approve", rationale="Evidence in the bundle supports the claim", findings=[],
    link_assessments=None)))
lab.cmd("planner-1", "planner", "review.finalize", assignment=assigned["assignment"],
        response=response, status="completed", usage={})
lab.cmd("reviewer-R2", "reviewer", "review.submit", assignment=assigned["assignment"],
        response=response, expected_basis=basis_b)
print("B next_action:", lab.next_action(claim_b)["action"])
paper = lab.cmd("writer-1", "writer", "paper.build", title="Effect X",
                claims=[claim_b], expected_bases={claim_b: basis_b})
```

Наблюдение:

```text
A next_action: replan
bundle mentions claim A: False | mentions R1: False
B next_action: paper_candidate
paper: paper-7342129a94a84dcb | draft mentions A or its rejection: False False
A still has open obligations: 1
```

**Исправление.** В `_next_action` (kernel.py:827-857) и повторно в `PaperBuilder.build` (reporting.py:445-448) считать veto и открытые obligations по «семейству» claim: все claims на том же протоколе и в его линии (`parent`, `replan_followup`) либо с пересекающимися evidence runs. В `Kernel._claim` требовать явную связь `supersedes`/`limits` для нового claim на протоколе, где у прежнего claim есть открытое veto или obligation; тогда сработают существующие правила контекста и acknowledgement. В manifest слепого review передавать хотя бы факт открытых findings у sibling claims.

#### A-02. Резолюция obligation, выданная для узкого child-claim, разблокирует соседний claim

- **Severity: high.** Мнение исходного reviewer «удовлетворён узким выводом C1» переносится на другой claim C2 с более сильным текстом и outcome, который этот reviewer не оценивал. Paper для C2 при этом показывает цепочку резолюции как легитимное закрытие.
- **Предусловия:** analyst; исходный reviewer выдал `replanning.resolve_obligation` для C1; любой не-contributor reviewer одобрил C2.
- **Классификация:** bug. ADR 0010:7 описывает резолюцию как одобрение «нового claim с зафиксированными scope и limitations»; название теста `test_effective_resolution_unblocks_only_the_reviewed_child` расходится с поведением, но тест проверяет только исходный claim.
- **Где:** `open_obligations`, `src/episteme/replanning.py:140-148` (сравнивается только `status`, не `resolution.payload.claim`); `_paper_followup_lineage`, `src/episteme/reporting.py:258-329` (не требует `dp["claim"] == claim`).

PoC (`poc_r4_resolution_transfer.py`, наследует `tests.test_resolution.SoleObligationResolutionTests`):

```python
class ResolutionTransfer(SoleObligationResolutionTests):
    def test_sibling_inherits_resolution(self):
        child, basis, results = self.complete_child()          # узкий, "inconclusive"
        self.resolve(child, basis, results)                     # исходный reviewer удовлетворён
        c = Kernel._get(self.store.events(), child, "claim")["payload"]
        broad = Kernel(self.store, self.analyst).claim(protocol=c["protocol"],
            statement="The broad mechanism is confirmed", scope=self.scope,
            evidence=c["evidence"], limitations=["none of note"], outcome="supports")
        friendly = Kernel(self.store, Actor("friendly-reviewer", "reviewer"))
        broad_basis = friendly.gate(broad)["basis_hash"]
        friendly.review(broad, verdict="approve", rationale="ok", actions=[],
                        expected_basis=broad_basis)
        print("source claim next_action:", friendly.next_action(self.claim)["action"])
        print("broad sibling next_action:", friendly.next_action(broad)["action"])
        PaperBuilder(self.store, Actor("writer", "writer")).build(
            title="Broad", claims=[broad], expected_bases={broad: broad_basis})
```

Наблюдение:

```text
resolution status: reviewer_satisfied | granted for claim: True
source claim next_action: replan
broad sibling next_action: paper_candidate
paper built for broad sibling: paper-4edd591c932747f9
```

**Исправление.** В `open_obligations`/`_next_action` засчитывать резолюцию только для claim из `review_obligation_resolution.payload.claim` (либо для claim, который тот же reviewer явно одобрил и который связан с C1 принятой `supersedes`). В `_paper_followup_lineage` требовать `dp["claim"] == claim`. Добавить регрессионный тест с sibling claim.

#### A-03. Amendment pack-bound протокола снимает потолок силы claim и pack-only допуск

- **Severity: high.** Обходит усиление, которое ADR 0016 ввёл именно против legacy-обхода потолка: одна дополнительная команда даёт gate-passing claim `supports` на runs, которые никогда не исполнялись.
- **Предусловия:** CLI caller, объявляющий planner, executor, replicator и analyst.
- **Классификация:** недокументированный разрыв. ADR 0016:262: «На pack-bound protocol `kernel.claim` и ручной `kernel.start_run` отвергаются … Иначе legacy-команды обходили бы потолок»; amendments pack-bound протоколов не упомянуты. Сами caller-declared outputs unmanaged runs — документированное legacy-поведение (ADR 0005:75 защищает только managed runs); разрыв в том, что этот путь достижим из pack-bound линии.
- **Где:** `kernel.py:455-458` (guard сравнивает только `pack_binding.protocol == protocol`), `batch.py:298-299` (`validate_start` проверяет тот же единственный протокол), `kernel.py:228-231` (amendment через `parent=` допускается без привязки), `domain_packs.py:259` (`pack.preregister` создаёт только корневые протоколы, своего пути amendment у пакетов нет).

PoC (`poc08_amendment_escape.py`, реальный `pack.preregister` для `synthetic_causal_v1`; `harness` повторяет helpers `tests/test_pack_workflow.py`):

```python
es, protocol, binding = harness.bound(store)
P = Kernel._get(store.events(), protocol, "protocol")["payload"]
child = harness.command(store, "kernel.preregister_for_set", dict(explanation_set=es,
    design=P["design"], metric=P["metric"], analysis_plan=P["analysis_plan"],
    stopping_rule=P["stopping_rule"], seeds=P["seeds"], run_limit=P["run_limit"],
    implementation=P["implementation"], environment=P["environment"], data=P["data"],
    replication_tolerance=P["replication_tolerance"], parent=protocol,
    statistical_design=P["statistical_design"], amendment_reason="audit amendment", seen_data=[]))
raw, metrics = store.put(b"fabricated raw bytes; no process ran"), store.put_json({P["metric"]: 3.14})
log = store.put(b"no execution")
run1 = harness.command(store, "kernel.start_run", dict(protocol=child, seed=7,
    implementation=P["implementation"], environment=P["environment"], command=["python", "p.py"]), actor=E)
harness.command(store, "kernel.finish_run", dict(run=run1, status="completed",
    outputs=dict(raw_data=raw, metrics=metrics, log=log)), actor=E)
run2 = harness.command(store, "kernel.start_run", dict(protocol=child, seed=7,
    implementation=execution_fields(store, binding)["reanalysis_implementation"],
    environment=P["environment"], command=["python", "r.py"], replicate_of=run1), actor=R)
harness.command(store, "kernel.finish_run", dict(run=run2, status="completed",
    outputs=dict(raw_data=raw, metrics=metrics, log=log)), actor=R)
claim = harness.command(store, "kernel.claim", dict(protocol=child,
    statement="The synthetic treatment robustly increases Y.", scope=P["scope"],
    evidence=[run1, run2], limitations=["fixture"], outcome="supports",
    inference_mode="exploratory"), actor=harness.ANALYST)
```

Наблюдение:

```text
parent is pack-bound: True
[parent] kernel.claim -> claims on a pack-bound protocol are admitted only by pack.analyse
child has a pack_binding: False
[child] validate_start(manual run) -> ALLOWED (legacy path open)
[child] claim recorded: claim-65f50ff426b24720 | outcome: supports | mode: exploratory
[child] mechanical gate passed: True | failures: []
[child] any pack_analysis/ceiling for this claim: False
```

На тех же данных пакетный путь ограничил бы outcome значением `inconclusive` (нет confidence interval и effect size).

**Исправление.** В `Kernel._preregister` отвергать `parent`, в линии которого есть `pack_binding`, если child создаётся не через `pack.preregister`; в `_claim` и `validate_start` обходить линию `parent`; отвергать legacy-протоколы, переиспользующие implementation/data digests, закреплённые pack binding; зафиксировать правило в ADR 0016.

#### A-04. `analysis.apply` допускает proposal, который frozen-адаптер не выдавал

- **Severity: high.** Аналитик подаёт собственный proposal (outcome `supports`, причинный statement) под ID и digest исходника адаптера. Событие `batch_analysis` выглядит как вывод адаптера, а слепой manifest исключает proposal и исходник адаптера (`review_assignment.py:23-32, 89-92`), поэтому reviewer не может сверить claim с тем, что адаптер реально вычислил.
- **Предусловия:** analyst через `CommandService` (обычная роль).
- **Классификация:** недокументированный разрыв. ADR 0013:16 утверждает, что адаптер `synthetic_causal_v1` «возвращает только `inconclusive`/`exploratory`», но допуск этого не обеспечивает. ADR 0013:18 и `batch_analysis.py:3` («adapter is a trusted local caller»; digest исходника — «provenance, не attestation») документируют границу лишь частично. Pack-путь при допуске перезапускает закреплённые хуки и сверяет envelopes (`domain_packs.py:901-907`, ADR 0016:260); legacy-путь — нет.
- **Где:** `BatchAnalysis.apply`, `src/episteme/batch_analysis.py:225-258`; `_context` (94-138) проверяет только форму `_proposal` (31-54) и происхождение адаптера `_origin` (57-91).

PoC (`poc_r11_analysis_apply_forged_proposal.py`, fixture `tests/test_batch_analysis.py` с реальным локальным исполнением batch):

```python
class ForgedProposal(BatchAnalysisTests):
    def test_forged(self):
        batch = self._complete()
        state = batch_index(self.store, self.store.events())[batch]
        honest = self.adapter.propose(self.store, state)
        forged = dict(honest, outcome="supports",
                      statement="The treatment causes the outcome in this population.")
        source = self.store.put(Path(inspect.getfile(type(self.adapter))).read_bytes())
        result = CommandService(self.store).execute(dict(
            context=dict(command_id=f"forged-{uuid4().hex}", expected_revision=len(self.store.events()),
                         actor=self.analyst.id, role="analyst", study_id=self.fixture.study,
                         correlation_id="poc", causation_id=None),
            request=dict(version=1, action="analysis.apply", payload=dict(
                batch=batch, expected_settlement=state["settlement"]["id"], proposal=forged,
                adapter_source_digest=source, reviewer_actor=self.reviewer))))
```

Наблюдение:

```text
adapter would propose: inconclusive / exploratory
admitted claim: supports / exploratory | The treatment causes the outcome in this population.
gate passed: True
```

**Исправление.** В `BatchAnalysis.apply` повторно вычислять proposal зарегистрированным адаптером с проверенным digest исходника (по образцу `run_hooks` в `PackAnalysis.analyse`) и требовать точного совпадения; иначе отказывать или явно помечать claim `proposal_origin=caller_submitted` и показывать reviewer вывод адаптера. Ограничить число кандидатов на batch либо наследовать между ними veto (см. A-01).

#### A-05. «Ящик стола» через перерегистрацию протокола: противоречащие попытки скрыты от reviewer и paper

- **Severity: high.** Неудачная или противоречащая попытка на тех же bytes под другим протоколом остаётся в журнале, но не попадает ни в слепой bundle рецензента, ни в paper, ни в limitations claim. Для legacy-протоколов её нет даже в evidence basis; для попытки на других bytes (тот же вопрос и hypotheses) — тоже.
- **Предусловия:** planner + executor; честный reviewer, работающий по слепому bundle.
- **Классификация:** недокументированный разрыв. Противоречит ADR 0002:27 («Failed foreign attempts остаются контекстом review»), README.md:173 («gates на полноту всех результатов») и README.md:163. Отсутствие study-level ledger попыток — документированный открытый пункт (ADR 0005:89), но сокрытие в paper и в manifest не документировано.
- **Где:** typed — `protocol_exposures` (`kernel.py:44-92`) добавляет чужие попытки в basis, но `_manifest` берёт runs только протоколов claims контекста (`review_assignment.py:70-83`), а `PaperBuilder.build` — только протокола claim (`reporting.py:478`; экспорт `report.md` при этом показывает все runs, `reporting.py:422-423`). Legacy — `protocol_exposures` возвращает `[]` без `statistical_design` (`kernel.py:53-54`).

PoC (`poc_r7_file_drawer.py`, fixture `tests.test_scientific_workflow.ScientificWorkflowTests`):

```python
def run_case(self, typed):
    design = {} if typed else dict(statistical_design=None)
    first = self.protocol(**design)
    bad = self.executor.start_run(first, seed=7, implementation=self.code,
                                  environment=self.environment, command=["x"])
    self.executor.finish_run(bad, status="completed", outputs={"raw_data": self.data,
        "metrics": self.store.put_json({"mean_difference": -9}),
        "log": self.store.put(b"contradicting result")})
    second = self.protocol(**design)                    # перерегистрация, те же данные
    claim = self.claim_fixture(second, outcome="supports")
    history = self.store.events()
    basis = self.planner.gate(claim)["basis_hash"]
    evidence, _ = Kernel(self.store, Actor("o", "observer"))._local_evidence(history, claim)
    manifest = json.dumps(_manifest(self.store, history, claim=claim, reviewer_actor="rev",
                                    expected_basis=basis, study_id="s"))
    self.reviewer.review(claim, verdict="approve", rationale="fixture", actions=[],
                         expected_basis=basis)
    paper = PaperBuilder(self.store, Actor("w", "writer")).build(
        title="T", claims=[claim], expected_bases={claim: basis})
```

Наблюдение:

```text
typed=True: bad run in basis=True | in blind manifest=False | in paper=False | first protocol in paper=False
typed=False: bad run in basis=False | in blind manifest=False | in paper=False | first protocol in paper=False
```

Вариант для pack-пути (`poc07_cross_protocol_rerun.py`): batch протокола A оставлен `unknown` (worker не стартовал), идентичный протокол B исполнен и проанализирован через `advance_pack_analysis`.

```text
A batch: unknown | A slots: [('primary:7', 'unknown'), ('reanalysis:7', 'waiting_primary')]
B batch: completed | same input bytes: True
A's unknown run in B's claim basis: True
A's unknown run in B's paper evidence table (reporting.py:478 selection): False
B claim limitations mention protocol A / its attempt: False
A batch still open (unsettled): True
C's abandoned run in B's claim basis: False
```

**Исправление.** Включать `protocol_exposures(history, plan)` (protocols/runs/results) в `observed_runs` manifest с пометкой `foreign_attempt` и в отдельный раздел paper «Prior and foreign attempts on the same data»; для legacy-протоколов и попыток на других bytes показывать в paper и gate другие протоколы с тем же explanation set или тем же набором hypotheses без связи `parent`; ввести study-level ledger попыток или явное событие `batch.abandon`, которое claim обязан цитировать.

#### A-06. Legacy `kernel.review` и CLI `review`: verdict без assignment, снятие чужого veto подменой ID, paper из demo-фикстуры

- **Severity: high** по эффекту; это **документированная известная проблема** поверх by-design caller-declared identity.
- **Предусловия:** CLI caller с произвольными actor strings.
- **Классификация:** документированная известная проблема и by-design граница: ADR 0016:211 («известные проблемы review integrity (снятие veto тем же reviewer в `kernel.py`, legacy `kernel.review` и CLI `review` без assignment)»), ADR 0011:15, семантика снятия veto тем же actor — ADR 0003:89; `kernel.py:1-4`, README.md:209. Новое наблюдение: paper, собранный из demo после такого approval, одновременно утверждает «This scaffold records reviewed claims» и сохраняет ограничение demo «No scientific review has been performed; no reviewer approval is fabricated». Это противоречит пункту контракта «Do not manufacture scientific reviewer approvals for demo output»: сам demo approval не фабрикует, но система делает фабрикацию однострочной и не отказывает в paper для `mode=synthetic_demo`.
- **Где:** `commands.py:142-143` (`kernel.review`, `kernel.review_with_links`), `commands.py:119` (`replanning.record_review`), `cli.py:264-273`; veto — `kernel.py:849-857`; `PaperBuilder.build` не проверяет synthetic scope (`reporting.py:440-448`).

PoC (`poc_r2_cli_impersonated_withdrawal.py`, реальный CLI в subprocess):

```python
cli("demo")
claim = next(c["id"] for c in cli("inspect")[1]["claims"])
basis = cli("gate", claim)[1]["basis_hash"]
def review(who, verdict, actions):
    f = root.parent / f"{who}-{verdict}.json"
    f.write_text(json.dumps(dict(reviewer_id=who, verdict=verdict, rationale="r",
                                 actions=actions, expected_basis=basis)), encoding="utf-8")
    return cli("review", claim, "--input", str(f))[1]["next_action"]["action"]
print("dr-strict rejects        ->", review("dr-strict", "reject", ["re-run with controls"]))
print("someone else approves    ->", review("friendly", "approve", []))
print("'dr-strict' approves     ->", review("dr-strict", "approve", []))
code, out = cli("paper", claim, "--title", "Planted slope", "--actor", "w")
```

Наблюдение:

```text
dr-strict rejects        -> replan
someone else approves    -> replan
'dr-strict' approves     -> paper_candidate
paper exit 0 internal_draft paper-2d25436d7fa7471d
```

**Исправление.** (1) Для `_next_action`/`PaperBuilder` засчитывать только reviews с receipt `review.submit` (assignment → dispatch → response); legacy reviews показывать как advisory; CLI `review` перевести на этот путь или удалить. (2) Аутентифицировать actor (отдельный write-service/OS identity — план M4). (3) `PaperBuilder` должен отказывать claims со scope `mode=synthetic_demo` без явного fixture-флага; статичное ограничение demo «no reviewer approval is fabricated» убрать или формулировать условно.

### 3.2. Medium

#### A-07. Повтор seed до «успеха»; метрики failed-попытки скрыты в paper

- **Severity: medium.** Неблагоприятный результат можно пометить `failed` после просмотра метрик и перезапустить тот же seed; gate это не сигнализирует, stopping rule не проверяется, а paper рисует failed-попытку прочерками, хотя её метрики записаны.
- **Предусловия:** executor (legacy run path); в managed execution — автор программы (статус определяется exit code).
- **Классификация:** недокументированный разрыв. Ручные повторы допустимы по ADR 0005:29, сверка stopping rule с outputs не реализована по ADR 0002:33, но скрытие записанных метрик failed-попыток в paper и отсутствие сигнала о повторе seed не документированы.
- **Где:** `kernel.py:363-398` (нет лимита попыток на seed, кроме `run_limit`), `kernel.py:420-431` (статус объявляет caller), `kernel.py:690-691` и `711-712` (non-completed не участвуют), `reporting.py:192-194`.

PoC (`poc_r6_retry_and_copied_reanalysis.py`, первая часть):

```python
protocol = lab.cmd("planner-1", "planner", "kernel.preregister", hypotheses=hyp, scope=scope,
    design="d", metric="mean", analysis_plan="a", stopping_rule="One attempt per seed; no retries",
    seeds=[7], run_limit=4, implementation=code, environment=env, data=raw,
    replication_tolerance=0.0)
def attempt(mean, status, reason=""):
    run = lab.cmd("exec-1", "executor", "kernel.start_run", protocol=protocol, seed=7,
                  implementation=code, environment=env, command=["x"])
    out = dict(raw_data=s.put(f"raw {mean}".encode()), metrics=s.put_json({"mean": mean}),
               log=s.put(b"log"))
    lab.cmd("exec-1", "executor", "kernel.finish_run", run=run, status=status,
            outputs=out, reason=reason)
    return run, out
attempt(-5.0, "failed", "numerical instability")      # результат увиден, затем «failed»
good, out = attempt(3.0, "completed")
```

Наблюдение (после реанализа из A-08, claim, review и `paper.build`):

```text
gate passed: True []
| run-4dbe163dee214304 | failed | 7 | mean | — | — | — |
| run-2a77f20ac8004f8a | primary | 7 | mean | 3.0 | [raw](...) | [source](...) |
| run-2e8d202adfd647e5 | reanalysis of same data | 7 | mean | 3.0 | [raw](...) | [source](...) |
draft shows the -5.0 result: False
```

**Исправление.** В `_gate_local` считать primary-попытки на seed и при повторе выдавать failure либо явное поле `retried_seeds` в gate/next_action/paper; для typed `fixed_sample` запрещать вторую primary-попытку на seed без amendment. В `_run_table` показывать записанные metrics и причину для failed/cancelled. Требовать, чтобы claim перечислял все терминальные попытки протокола, а не только completed.

#### A-08. «Независимый реанализ» копированием digests оригинала (legacy-путь)

- **Severity: medium.** Gate засчитывает «independent reanalysis», хотя ничего не пересчитано: replicator с другой строкой ID и implementation, отличающейся одним байтом, цитирует metrics/raw_data digests оригинала.
- **Предусловия:** CLI caller (роль replicator), не pack-bound протокол, unmanaged run.
- **Классификация:** документированная by-design граница: README.md:209 («Разные ID и source hashes не доказывают независимость рассуждения или clean-room реализацию»), implementation-review.md:51,53, ADR 0005:75 (произвольные blobs запрещены только для managed runs). Однако gate (`"independent reanalysis missing for primary evidence"`), next_action и paper не различают outputs, наблюдённые worker, и outputs, объявленные caller.
- **Где:** `kernel.py:380-391` (разные ID и bytes), `kernel.py:420-431` (любые outputs), `kernel.py:694-704` (сверяются только `raw_data` и tolerance).

PoC (вторая часть того же файла):

```python
replica = lab.cmd("exec-1b", "replicator", "kernel.start_run", protocol=protocol, seed=7,
                  implementation=s.put(b"primary code "), environment=env, command=["x"],
                  replicate_of=good)
lab.cmd("exec-1b", "replicator", "kernel.finish_run", run=replica, status="completed",
        outputs=out)                                  # digests оригинала, ничего не вычислено
```

Наблюдение: `gate passed: True []`, в paper строка `reanalysis of same data | 3.0`.

**Исправление.** Помечать provenance evidence (`managed`/`caller_declared`) в gate, next_action и таблице paper; дать протоколу политику «все runs только managed»; дешёвый tripwire — отклонять реанализ, чей `metrics` digest совпадает с оригиналом; в перспективе — пересчёт метрики доменным checker (`recompute_metrics` ADR 0016) и для legacy-протоколов.

#### A-09. Exploratory → confirmatory через перекодированную копию просмотренных данных

- **Severity: medium.** Документированная граница, но эксплуатируется одним байтом и даёт confirmatory claim до `paper_candidate`; confirmatory runs при этом читают те же уже просмотренные bytes без всякой маскировки.
- **Предусловия:** planner + executor.
- **Классификация:** документированная by-design граница: ADR 0002:15 («эквивалентные копии с другим digest … не устанавливаются»), ADR 0002:33 (нет «соответствия raw output заявленному split»), `protocols.py:4`. README.md:172 («запрет повторного объявления просмотренных bytes свежим holdout») верен буквально только для идентичных bytes.
- **Где:** `kernel.py:287-316`, `kernel.py:341-345`.

PoC (`poc_r5_confirmatory_relabel.py`, fixture `ScientificWorkflowTests`):

```python
exploratory = self.protocol()                     # discovery split = self.holdout
self.execute_run(exploratory)                     # inputs теперь считаются просмотренными
copy = self.store.put(self.store.read(self.holdout) + b"\n")   # те же наблюдения
confirmatory = self.protocol(design=self.design("confirmatory", split=copy))
claim = self.claim_fixture(confirmatory, outcome="supports")    # runs снова читают self.data
```

Наблюдение:

```text
exposed: cdf0081ec908 -> 'fresh' copy: 2cdbb7c14144
claim inference_mode: confirmatory | gate passed: True
next_action: paper_candidate
```

**Исправление.** Дешёвые tripwires: для confirmatory claim — gate failure, если `raw_data` или вход любого run входит в `seen_data` протокола; привязать вход managed run к digest confirmatory split. Доменный hook канонического fingerprint содержимого (pack) для обнаружения перекодированных копий. Уточнить README.md:172: «byte-identical».

#### A-10. Read side доверяет любому событию `review`: самоодобрение contributor через `Store.append`

- **Severity: medium.** `next_action`, `PaperBuilder` и Graph не перепроверяют предикат допуска review (роль, независимость, receipt). Событие, дописанное в обход команд, с ролью `executor` от автора primary run засчитывается как approval.
- **Предусловия:** in-process caller с handle `Store` (legacy API), in-process код адаптера/хука пакета или владелец файлов.
- **Классификация:** частично документированная граница (implementation-review.md:51: нельзя давать недоверенным агентам `Store.append`; `commands.py:3-4`), но `paper.build` — сохраняемое решение, а контракт требует «validate again at a persisted transition». Graph (README.md:192: «проверяет ссылки») не замечает review от contributor с ролью executor.
- **Где:** `kernel.py:849-857`, `reporting.py:445-448`, `graph.py:664-667` (проверяется только basis).

PoC (`poc_r9_raw_review_event.py`):

```python
claim = lab.basic_claim_world()
basis = lab.basis(claim)
lab.store.append(id="review-selfmade", kind="review", actor="exec-1", role="executor",
                 payload=dict(claim=claim, verdict="approve", rationale="looks fine to me",
                              actions=[], basis_hash=basis),
                 expected_revision=len(lab.store.events()))
print("next_action:", lab.next_action(claim)["action"])
paper = lab.cmd("writer-1", "writer", "paper.build", title="T", claims=[claim],
                expected_bases={claim: basis})
graph = ResearchGraph.from_store(lab.store)
```

Наблюдение:

```text
next_action: paper_candidate
paper: paper-5f9ec280d1594515 | graph verified, nodes: 18 | receipts cover the review: False
```

**Исправление.** В `_next_action`, `PaperBuilder` и Graph засчитывать только reviews с `role == "reviewer"`, автором вне contributors на префиксе до review и (предпочтительно) с receipt одной из review-команд, то есть replay-валидировать их так же, как `_index`-функции валидируют obligations и assignments.

#### A-11. Вредоносный DomainPack: обход статической проверки, доступ к живому Store и скрытым inputs, подмена потолка в процессе

- **Severity: medium** (по ущербу — high; понижено, потому что malicious pack явно вне scope).
- **Предусловия:** автор пакета (для регистрации пакет добавляется в `registry.PACKS`; в PoC — monkeypatch только внутри процесса PoC).
- **Классификация:** документированная by-design граница, документация честна: ADR 0016:61 («Пакет может читать Store, файлы и сеть, ошибиться или солгать … Malicious pack, malicious executor и утечка oracle через пакет — вне scope»), ADR 0016:247 («проверка контракта для доверенного кода, а не граница безопасности»), `domains/registry.py:8-10`, `domains/api.py:1019-1024` (`CasView` — «not a security boundary»). Две оговорки: ADR 0016:247 утверждает, что вызовы `__import__/eval/exec/compile` «отвергаются», — это верно только для вызова по голому имени; а разрешённый фасад сам открывает `os` (`api.canonical.__globals__["os"]`), так что даже честный пакет может случайно нарушить правило «без окружения и файлового I/O». ADR 0016:265 называет host inputs скрытыми от hooks анализа — это верно только для чтения через `CasView`.
- **Где:** `domains/registry.py:120-156` (`import_violations` видит только `ast.Import`, `ast.ImportFrom` и вызовы по имени), `domains/registry.py:45-60` (deny lists), `domains/registry.py:166-169` (загрузчик исполняет `exec` в процессе ядра); хуки контроллера — `analysis_controller.py:263`, `ceiling` разрешается как глобальное имя модуля при вызове (`domain_packs.py:724`).

PoC (`poc01_import_bypass.py`, `malpack/malicious_pack_v1`):

```python
cases = {"alias __import__": b"f = __import__\nos = f('os')\n",
         "builtins subscript": b"os = __builtins__['__import__']('os')\n",
         "getattr builtins": b"g = getattr(__builtins__, '__imp'+'ort__')\n",
         "alias exec": b"e = exec\ne('import os')\n",
         "api.__globals__ reach": b"from episteme.domains import api\nos = api.canonical.__globals__['os']\n",
         "allowed stdlib w/ os": b"import sysconfig\nenv = sysconfig.os.environ\n",
         "baseline (flagged)": b"import os\n"}
for label, src in cases.items():
    print(label, registry.import_violations({"__init__.py": src}))
# внутри пакета (проходит контракт):
_imp = __builtins__["__import__"] if isinstance(__builtins__, dict) else getattr(__builtins__, "__import__")
def steal(digest_hex):
    gc, Store = _imp("gc"), _imp("episteme.store", fromlist=["Store"]).Store
    stores = [o for o in gc.get_objects() if isinstance(o, Store)]
    return {"found_live_stores": len(stores), "blob": stores[0].read(digest_hex).decode()}
```

Наблюдение (всего проверено 9 форм обхода, все вернули `[]`):

```text
import_violations['api.__globals__ reach'] = []
import_violations['baseline (flagged)'] = ['__init__.py:1: import os']
loaded pack: malicious_pack_v1 e785e8974fe2818c module: _episteme_pack_malicious_pack_v1_...
hook result: {'found_live_stores': 1, 'blob': '{"world":{"treatment_effect":1.5,"HIDDEN":"ground truth"}}', ...}
```

Дополнительно `poc05_ceiling_subversion.py` показывает механизм подмены `domain_packs.ceiling` кодом в том же процессе: после подмены живой допуск получает `confirmatory ['supports', 'refutes', 'inconclusive']`. Полный путь через фальшивый batch не прогонялся; replay в чистом процессе (`_analysis_index`, `domain_packs.py:812-871`, его используют `graph`, export и `pack verify`) пересчитывает потолок и отвергает подделку.

**Исправление.** Не считать статическую проверку границей; если её сохранять — флагировать доступ к `__builtins__`, `getattr` над builtins, атрибуты `__globals__`/`__dict__`/`__subclasses__` и сузить stdlib allowlist. Реальное исправление — исполнять хуки в subprocess/контейнере (зарезервированный `hook_isolation`) и повторять допуск в чистом интерпретаторе; уточнить формулировку ADR 0016:247.

#### A-12. Программа исполнителя: полное окружение, запись вне workspace, перепись хранилища, которое её оценивает

- **Severity: medium** (по ущербу — high: любая программа эксперимента или реанализа получает необнаруживаемую запись в научный журнал; понижено, потому что malicious executor явно вне scope, ADR 0016:61).
- **Предусловия:** автор программы исполнителя (на pack-пути — автор пакета; на legacy-пути — автор implementation протокола).
- **Классификация:** документированная by-design граница, последствия не описаны. ADR 0005:9 («не запрещают программе читать файлы, сеть, environment»), ADR 0016:16 («наследуемое окружение, без sandbox»), README.md:98, architecture.md:150. Не сказано, что «отдельный рабочий каталог» ADR 0005:7 находится внутри корня хранилища (`<root>/executions/<token>`), так что `../../state.sqlite3` — живой журнал; целевое ограничение роли Executor «Нет записи в preregistration/reviews/accepted claims» (architecture.md:121) не обеспечивается; `env_allowlist` зарезервирован, но в v1 обязан быть `null` (`domains/api.py:432`).
- **Где:** `execution.py:354` (workspace под `store.root / "executions"`), `runner_backend.py:239` и `execution.py:381` (`Popen` без `env=`, `cwd=workspace`).

PoC A (`poc02_executor_env_escape.py`, реальные `execution.enqueue` и `work_job`; секрет — подложенная канарейка):

```python
os.environ["EPISTEME_FAKE_SECRET"] = "sk-live-AUDIT-PLANTED-TOKEN-do-not-ship"
PROGRAM = b'''import json, os, pathlib
stolen = os.environ.get("EPISTEME_FAKE_SECRET", "<absent>")
escape = pathlib.Path(os.getcwd()).parent.parent / "escaped_outside_job_workdir.txt"
escape.write_text("written by executor program outside its workspace; secret=" + stolen)
pathlib.Path("raw.bin").write_bytes(b"raw")
pathlib.Path("metrics.json").write_text(json.dumps({"value": 1.0, "exfiltrated_secret": stolen}))'''
```

```text
job status: completed
metrics.json recorded in CAS: {'value': 1.0, 'exfiltrated_secret': 'sk-live-AUDIT-PLANTED-TOKEN-do-not-ship', ...}
file written OUTSIDE job workspace exists: True
```

PoC B (`poc11_executor_program_tamper.py`): программа во время запуска нейтрализует triggers журнала, после чего удаление её же результата никто не замечает.

```python
PROGRAM = b'''import json, pathlib, sqlite3, sys
pathlib.Path("raw.json").write_bytes(pathlib.Path(sys.argv[1]).read_bytes())
pathlib.Path("metrics.json").write_text(json.dumps({"mean": 2.0}))
db = sqlite3.connect("../../state.sqlite3")      # журнал, который будет оценивать этот run
for name, op, table in (("events_no_update", "UPDATE", "events"), ("events_no_delete", "DELETE", "events"),
                        ("command_receipts_no_update", "UPDATE", "command_receipts"),
                        ("command_receipts_no_delete", "DELETE", "command_receipts")):
    db.executescript(f"DROP TRIGGER {name}; CREATE TRIGGER {name} BEFORE {op} ON {table} BEGIN SELECT 1; END;")
'''
```

```text
job status after run: completed
trigger body now: CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT 1; END
tampered store passes CLI inspect/graph/backup: 0 0 0
plain DELETE of the finalize command accepted; job status now: unknown
CLI inspect/graph/backup exit after erasing the completed result: 0 0 0
```

**Исправление.** Передавать `env=` по allowlist (реализовать `env_allowlist`); вынести workspaces за пределы корня хранилища; до запуска запоминать head/revision и снимок `sqlite_master`, после выхода worker и при finalize сверять и отказывать при изменениях; в M2 — OS-изоляция (restricted token/AppContainer на Windows, отдельный UID и read-only mounts на POSIX). Явно описать в README и command-api, что программа может читать и менять хранилище.

#### A-13. Владелец файлов незаметно переписывает историю; `INSERT OR REPLACE` обходит append-only triggers без их удаления

- **Severity: medium.** Удаление отрицательного review (хвоста или середины цепочки с пересчётом hashes и receipts) не замечает ни одна команда, после чего второй reviewer доводит claim до paper. Сама граница документирована; новое — что для перезаписи даже не нужно трогать triggers.
- **Предусловия:** владелец файлов или любой код с SQL-доступом к БД (включая программу исполнителя, A-12).
- **Классификация:** граница документирована (`store.py:3-4`, architecture.md:79, mvp-plan.md:32, ADR 0001:69, recovery.md:76, README.md:209). **Bug** в части REPLACE: не выполняются architecture.md:77 («DB triggers предотвращают UPDATE/DELETE через обычные операции»), ADR 0001:85 («UPDATE/DELETE квитанций запрещены обычным SQL») и ADR 0001:63 («append-only triggers»). Переформулировки требует architecture.md:164 («Реализовано: immutable event history»).
- **Где:** `store.py:132-135` и `151-156` (есть только `BEFORE UPDATE`/`BEFORE DELETE`; при выключенных recursive triggers REPLACE удаляет старую строку без `BEFORE DELETE`), `store.py:206-220` (`events()` проверяет лишь внутреннюю согласованность), `store.py:289-334` (receipts только self-hashed).

PoC A (`poc01_tail_truncation.py`): удалён хвостовой отрицательный review и его receipt.

```python
db.executescript("""DROP TRIGGER events_no_delete; DROP TRIGGER command_receipts_no_delete;
    DELETE FROM events WHERE seq = (SELECT max(seq) FROM events);
    DELETE FROM command_receipts WHERE command_id = 'cmd-negative-review';""")
```

```text
before: next_action = replan
  inspect  exit=0 next_action=scientific_review
  gate     exit=0 passed=True
  graph    exit=0 revision=16
  receipts exit=0 receipts=0
  pack     exit=0 status=matched
  backup   exit=0 / restore exit=0
after: second reviewer approves -> exit 0 paper_candidate
paper build -> exit 0 internal_draft
triggers now: ['command_receipts_no_delete', 'command_receipts_no_update', 'events_no_delete', 'events_no_update']
```

`CREATE TRIGGER IF NOT EXISTS` при следующем writable открытии молча восстанавливает удалённые triggers и стирает след. Вариант `poc01b_midhistory_rechain.py` удаляет review из середины, пересчитывает цепочку и self-hashed receipts: все команды завершаются с кодом 0, итог `paper_candidate`.

PoC B (`poc02_insert_or_replace.py`): triggers на месте, хвостовой reject заменён на approve; receipt заменён так, что replay той же команды возвращает подделку.

```python
row["payload"] = dict(json.loads(row["payload"]), verdict="approve", actions=[], rationale="Approved.")
db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
           (*[row[k] for k in list(row)[:7]], canonical(row["payload"]).decode(), row["previous_hash"],
            digest(canonical(row))))
receipt["result"] = "hypothesis-FORGED"
db.execute("INSERT OR REPLACE INTO command_receipts VALUES (?,?,?)",
           ("cmd-h1", canonical(receipt).decode(), digest(canonical(receipt))))
```

```text
blocked: UPDATE events SET acto... -> events are append-only
blocked: DELETE FROM events WHE... -> events are append-only
triggers still installed: ['command_receipts_no_delete', 'command_receipts_no_update', 'events_no_delete', 'events_no_update']
after REPLACE of tail review: inspect exit 0 next_action = paper_candidate
replay of the same envelope returns: hypothesis-FORGED
receipts CLI exit: 0 | graph CLI exit: 0
```

**Исправление.** Добавить `BEFORE INSERT` triggers, отвергающие существующие `seq`/`id`/`command_id` и разрывы последовательности (`NEW.seq != MAX(seq)+1`); проверено субагентом на копии: REPLACE и разрывы блокируются, обычный append проходит. Добавить регрессионный тест на REPLACE (validation.md:86 заявляет тесты «append-only triggers», но REPLACE не покрыт). Долгосрочно — внешний checkpoint head hash (уже запланирован) и отдельный write-service.

### 3.3. Low

#### A-14. Слепой контекст несовместим с обязательным acknowledgement чужих findings; reviewer блокируется на basis

- **Severity: low.** Для связанного контекста с открытым чужим отрицательным review approval через слепой путь невозможен без внешнего источника ID этого review, то есть без утечки прежнего verdict. После неудачной попытки повторное назначение того же reviewer на тот же basis запрещено («reviewer already assigned to this claim basis»), и остаётся только legacy `kernel.review`.
- **Предусловия:** нет (обычный сценарий).
- **Классификация:** недокументированное противоречие дизайна: ADR 0011:13 исключает прежние reviews из проекции, ADR 0003:95 требует их явного acknowledgement по ID.
- **Где:** `review_assignment.py:126-168` (в manifest нет reviews), `review_submission.py:84-88`, `review_assignment.py:236-240`.
- **PoC** (`poc_r8_blind_vs_acknowledge.py`): A отклонён R1, B `supersedes` A, R2 получает слепой bundle B и отвечает approve с evidence `[A, B]`.

```text
manifest has link: True | has R1 review id: False
blind approval rejected: approval must acknowledge open linked-context reviews
```

**Исправление.** Для связанного контекста включать в manifest ID (и при необходимости тексты) открытых context findings отдельным разделом политики v2 либо ввести шаг acknowledgement после первичного слепого мнения; разрешить новое assignment после неуспешной submission.

#### A-15. Restore более старого снимка: тихий откат и потеря идемпотентности команд

- **Severity: low.** Эквивалентно усечению хвоста (A-13), но выполняется штатной командой: каждая команда, подтверждённая клиенту после снимка, выполняется заново с другим результатом, а изменённое тело под уже использованным `command_id` принимается без conflict. Ничто не помечает восстановленный store как откат.
- **Предусловия:** оператор или владелец файлов, восстанавливающий старый снимок.
- **Классификация:** недокументированный разрыв для идемпотентности. Частично документировано (recovery.md:76: hashes «не предоставляют … независимый checkpoint истории»), но README.md:127 («Snapshot сохраняет … повторную доставку команд»), recovery.md:62 и command-api.md:27 не оговаривают, что это верно только для команд внутри снимка.
- **Где:** `recovery.py:185-212` (сверка только с self-attested manifest), `store.py:384-392` (поиск receipt — единственный источник replay).
- **PoC** (`poc04_rollback_fork.py`): `cmd-2` подтверждён в исходном store; снимок сделан до него.

```text
source: cmd-2 -> hypothesis-61b89bd0bca642d0 | replay -> hypothesis-61b89bd0bca642d0
source: changed body under cmd-2 -> ConflictError: command ID already used with a different request or context
restore accepted older snapshot: revision 1
restored: same cmd-2 envelope -> hypothesis-1779c986ae4b48ca | equal to acknowledged result: False
restored: changed body under cmd-2 -> hypothesis-f0d66d92c791448f (no conflict)
```

**Исправление.** Lineage epoch: случайный ID при init/restore в аддитивной таблице и в результатах команд; restore записывает `restored_from=(revision, snapshot_hash)`; клиент может передать последний известный `(revision, head_hash)`, и store отказывает, если отстаёт. Оговорить ограничение в recovery.md:62, README.md:127, command-api.md:27.

#### A-16. Нейтрализованные triggers переживают reopen, backup и restore

- **Severity: low.** Разовая компрометация (например, программой исполнителя, A-12) закрепляется во всех последующих backups и восстановленных копиях.
- **Предусловия:** владелец файлов или программа исполнителя.
- **Классификация:** недокументированный разрыв.
- **Где:** `store.py:132-135,151-156` (`IF NOT EXISTS` сохраняет любое тело trigger), `store.py:137-140` (единственная проверка схемы — наличие `schema_version`), `recovery.py:88-98` (`_state` выполняет `integrity_check`, chain и Graph, но не проверяет `sqlite_master`).
- **PoC** (`poc03_neutered_triggers.py`): triggers заменены одноимёнными `SELECT 1`.

```text
trigger body: CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT 1; END
backup manifest revision: 3
restore manifest revision: 3
restored copy: DELETE on events/receipts accepted; integrity_check = ok
restored store still verifies: 2 events, 2 receipts
```

**Исправление.** При каждом открытии и в `recovery._state` сравнивать нормализованный SQL `sqlite_master` (таблицы, индексы, triggers) с точным allowlist; отказывать при любом отличии, включая лишние triggers или views.

#### A-17. Graph читает receipts вне своего снимка событий: конкурентная запись выглядит как порча

- **Severity: low.** Fail-closed, но даёт ложные integrity alarms (`episteme graph` с кодом 2) при обычной конкурентной работе.
- **Предусловия:** нет; достаточно конкурентного writer.
- **Классификация:** bug. Противоречит `graph.py:14` («Construction verifies one event snapshot …») и обходит защиту, описанную в `store.py:339` и ADR 0001:63 («`receipts()` читает обе таблицы в одной SQL read transaction»).
- **Где:** `graph.py:243` (`store.events()` в autocommit) и `graph.py:859` (отдельное чтение `_verified_receipts`).
- **PoC** (`poc05_graph_receipt_race.py`): детерминированное чередование и 300 построений Graph при пишущем потоке.

```text
ResearchGraph.from_store: IntegrityError: command receipt corrupt: cmd-ResearchGraph: invalid command receipt event range
export_store: ok
Store.receipts: ok
real threads: 24/300 graph builds raised IntegrityError; first: ['command receipt corrupt: loop-16: invalid command receipt event range']
store afterwards verifies: 196 events, 196 receipts
```

**Исправление.** В `ResearchGraph.from_store` брать events и receipts в одной read-транзакции (как `Store.receipts()`) или добавить `Store.snapshot()`.

#### A-18. Hash chain покрывает разобранный JSON, а не сохранённый текст: дубликаты ключей

- **Severity: low.** Внешний аудит средствами SQLite (`json_extract` берёт первый из дублирующихся ключей) видит `approve`, а EpistemeOS (Python берёт последний) — `reject`. Все проверки проходят, неоднозначность переживает backup и restore; строки в середине истории переписываются без разрыва цепочки.
- **Предусловия:** владелец файлов (через REPLACE, без изменения triggers).
- **Классификация:** недокументированный разрыв; не выполняется буквальное прочтение implementation-review.md:34 («изменение actor/payload после обхода trigger обнаруживается по hash chain») для текста payload.
- **Где:** `store.py:212` (`json.loads` без отказа от дубликатов, без сравнения с каноническим текстом).
- **PoC** (`poc09_payload_text_malleable.py`): `row[7] = '{"verdict":"approve",' + canonical(json.loads(row[7])).decode()[1:]`, затем `INSERT OR REPLACE`.

```text
SQLite json_extract verdict: approve
Store.events() verifies 18 events; EpistemeOS verdict: reject | next_action: replan
after backup+restore, json_extract still: approve
CLI inspect/graph exit: 0 0
```

**Исправление.** В `Store.events()` разбирать с `object_pairs_hook`, отвергающим дубликаты, и требовать `row["payload"] == canonical(parsed).decode()` побайтно; это обратно совместимо (все строки, записанные `Store.append`, уже канонические — проверено на 16 строках).

#### A-19. `inspect` и `gate` не делают replay-проверку; `Store.receipts()` принимает поддельные receipts

- **Severity: low.** Обе команды завершаются с кодом 0 на истории, которую Graph, export и backup отвергают; `inspect` при этом заявлен как «Verify and inspect existing state» (`cli.py:28`). Подделка receipt-backed события (orphan или с self-hashed receipt) ловится семантическим replay — это отрицательный результат, см. раздел 5.
- **Предусловия:** владелец файлов.
- **Классификация:** недокументированный разрыв.
- **Где:** `reporting.py:61-62` (`inspect_store` проверяет только chain и считает gate/next_action), `kernel.py:600-716` (`_gate` не вызывает replay-индексы).
- **PoC** (`poc10_orphan_forged_receipt.py`): поддельный `review_assignment` с утечкой исходника в allowlist и self-hashed receipt.

```text
orphan event -> graph: graph: invalid review assignment history: review assignment lacks its original command receipt
Store.receipts() accepts the forged receipt: ['cmd-assign', 'cmd-forged']
CLI inspect exit=0
CLI gate    exit=0
CLI export  exit=2 {"error": "review assignment differs from its historical basis or bundle"}
CLI backup  exit=2 {"error": "graph: invalid review assignment history: ..."}
```

**Исправление.** Выполнять в `inspect` (и в `gate --strict`) ту же replay-валидацию, что и `ResearchGraph.build`, либо явно документировать, что provenance проверяют только `graph`, `export` и `backup`.

#### A-20. Повреждённый или «занятый» CAS-файл нельзя восстановить; один orphan блокирует backup

- **Severity: low.** Fail-closed, но один плохой файл в CAS останавливает disaster recovery; занятое имя digest (например, предсказуемого детерминированного output) блокирует будущую запись этих bytes.
- **Предусловия:** владелец файлов, программа исполнителя или сбой диска.
- **Классификация:** недокументированный разрыв; recovery.md:52 описывает проверку digest, но не эти последствия.
- **Где:** `store.py:177-179` (существующий путь только перепроверяется), `recovery.py:132-134` (каждый digest-файл должен проходить проверку, даже без ссылок).
- **PoC** (`poc06_cas_unrepairable.py`):

```text
put(damaged orphan) cannot repair/store correct bytes: artifact hash mismatch: 1bb05ed5...
put(squatted digest) cannot repair/store correct bytes: missing artifact: f4273bd4...
backup (snap) refuses the whole store: IntegrityError file hash mismatch: 1bb05ed5...
CLI inspect exit: 0 | CLI backup: {"error": "expected a plain file: ...f4273bd4..."}
```

**Исправление.** В `Store.put` переносить непроверяемую запись в `artifacts/quarantine/` и записывать самопроверяемые bytes; добавить `fsck`/repair; backup должен отдельно отчитываться о неиспользуемых повреждённых записях вместо отказа целиком.

#### A-21. Snapshot принимается как `--root` и портится; крах restore оставляет полуопубликованный каталог

- **Severity: low.**
- **Предусловия:** любой CLI caller, ошибочно указавший каталог snapshot; сбой или kill во время backup/restore.
- **Классификация:** частично документировано: recovery.md:70 («Обычное writable открытие Store умеет создавать БД и поэтому не служит способом ожидания готовности незавершённого destination») и recovery.md:72. Не документировано, что `export`/`command` молча мутируют snapshot (README.md:118 говорит только «не меняя журнал») и что оставшиеся `.episteme-recovery-*` — полные восстанавливаемые копии состояния, которые никто не убирает и не показывает.
- **Где:** `cli.py:235` и `cli.py:252` с `reporting.py:429`, `store.py:111-119` (writable open переводит БД в WAL); `recovery.py:54-64` (очистка только при штатном выходе), `recovery.py:107-114`.
- **PoC** (`poc07_snapshot_mutated.py`, `poc08_crash_leftovers.py`):

```text
export --root snap-a: exit 0 | members now: ['artifacts', 'events.jsonl', 'manifest.json', 'report.md', 'review-bundle.json', 'state.sqlite3']
restore snap-a: IntegrityError: unexpected snapshot members
restore snap-b: IntegrityError: file hash mismatch: state.sqlite3
restore process killed (exit 9); destination exists: True
leftover staging: .episteme-recovery-pte10qdd ['artifacts', 'manifest.json', 'state.sqlite3']
leftover backup staging restores as a complete snapshot: revision 3
half-published destination members: ['artifacts']
CLI command on it: exit 0; it now has 1 event(s) and 1 blob(s) from the original history
```

**Исправление.** Writable `Store` и пишущие команды CLI должны отказывать для корня с `manifest.json` формата `episteme-state-directory`; писать маркер `.incomplete` сразу после `mkdir` и удалять последним, а `Store` — отказывать при его наличии; `command` не должен инициализировать непустой каталог без явного `--init`; сообщать о старых `.episteme-recovery-*`.

#### A-22. Actor IDs не нормализуются; analyst может совпадать с planner или executor

- **Severity: low.** Пробел в конце или гомоглиф позволяет назначить contributor «независимым» reviewer; совпадение analyst с planner/executor не ограничено.
- **Предусловия:** CLI caller с произвольными actor strings.
- **Классификация:** документированная граница (caller-declared actors: `kernel.py:3-4`, `store.py:365`, ADR 0013:18) с разрывом: `_validate_analysis_request` (`domain_packs.py:784-785`) обрезает пробелы только для проверки на пустоту и затем сравнивает исходную строку.
- **Где:** `domain_packs.py:922-923`, `kernel.py:743`, `kernel.py:779-795`.
- **PoC** (`poc03_actor_overlap.py`, восстановленный pack batch и `advance_pack_analysis` для каждого варианта):

```text
[reviewer==analyst] REJECTED GateError: analysis reviewer contributed to evidence
[reviewer==planner] REJECTED GateError: analysis reviewer contributed to evidence
[analyst==planner] ACCEPTED status=awaiting_review
[analyst==executor] ACCEPTED status=awaiting_review
[reviewer-trailing-space] ACCEPTED status=awaiting_review   ("pack-analyst ")
[reviewer-cyrillic-a] ACCEPTED status=awaiting_review       ("pаck-analyst")
```

**Исправление.** Нормализовать actor IDs (strip, NFKC, casefold) перед каждой проверкой независимости и отвергать confusables; документировать, что обеспечивается только разделение reviewer и contributors.

#### A-23. «Read-only» `pack describe` и `pack verify` исполняют код пакета

- **Severity: low.** Команды, которые оператор считает безопасными (`describe` даже не открывает Store), исполняют код пакета со всеми побочными эффектами.
- **Предусловия:** автор пакета.
- **Классификация:** документированная граница с оговоркой: ADR 0016 описывает повторное исполнение хуков в `pack verify`; «read-only» в help CLI (`cli.py:94-96`) относится к Store, а не к исполнению кода, и это различие не оговорено.
- **Где:** `domain_packs.py:955-964` (`describe_pack` вызывает hook), `domain_packs.py:985-1027` (verify повторно исполняет compile и analysis hooks).
- **PoC** (`poc04_readonly_exec.py`): `describe()` вредоносного пакета пишет файл-маркер.

```text
marker file created by 'read-only' describe: True
marker content: describe() hook executed arbitrary code during a read-only command
verify runs compile hooks: True | verify runs analysis hooks (run_hooks): True
```

**Исправление.** Документировать; исполнять хуки изолированно (см. A-11).

## 4. Известные пункты из задания: подтверждение и точная оценка

1. **Veto берёт последнее мнение reviewer по всем basis** (`kernel.py:849-857`). Подтверждено: `latest = {e["actor"]: ...}` строится по всем reviews этого claim, а `reviews` для paper — только на текущем basis. Нетипизированное возражение снимается более поздним approval того же actor ID, включая подставленный ID (A-06). **Маскировка veto approval'ом на устаревшем basis невозможна**: все пути записи (`Kernel._record_review`, `review.submit`, `replanning.record_review`, `replanning.resolve_obligation`) пересчитывают gate и требуют `basis == текущий` в момент сохранения (`kernel.py:735-738`, `review_submission.py:66-69`, `resolution.py:105-109`), а basis монотонен при append-only истории. PoC `poc_r3_stale_approval.py` (после нового failed-run basis сменился, R1 отклонил claim на новом basis):

   ```text
   basis changed: True
   kernel.review on stale b1 -> rejected: stale review evidence bundle
   command kernel.review on stale b1 -> rejected: stale review evidence bundle
   next_action: replan
   after same-ID approval on b2: paper_candidate
   ```

   Реальная слабость veto — не basis, а привязка к ID claim (A-01, A-02) и к неаутентифицированной строке actor (A-06).
2. **Legacy `kernel.review` и CLI `review` пишут verdict без assignment** — подтверждено (`commands.py:142-143`, `cli.py:264-273`; так же `kernel.review_with_links` и `replanning.record_review`, `commands.py:119`). `next_action` и `PaperBuilder` не отличают такие reviews от пути assign → dispatch → submit. Документировано (ADR 0011:15, ADR 0016:211). Эффект — A-06.
3. **Reviewer под той же OS identity читает Store** — подтверждено и документировано (`reviewer_controller.py:1-6`, ADR 0011:19, ADR 0012:19). PoC `poc_r10_provider_reads_store.py`: provider получает только проекцию, но читает исключённые политикой прежние verdicts из `state.sqlite3`:

   ```text
   request contains prior verdict: False
   provider read from Store: ['SECRET-PRIOR-VERDICT: confounded']
   ```

4. **Статическая проверка импортов пакета — эвристика; хуки исполняются in-process внутри write-транзакции** — подтверждено и документировано (ADR 0016:61, 247, 260; `domains/registry.py:8-10`). Обход тривиален, последствия — A-11.
5. **Host inputs (synthetic world) лежат в CAS и receipts и не секретны** — подтверждено; README.md:35 («это не секретный sandbox») и ADR 0016:265 честны. Слепой review исключает их только из manifest (`domain_packs.py:155-170`), но не из Store; хук пакета читает их через живой Store (A-11).
6. **`analysis advance --adapter` — только утверждение** — подтверждено (`cli.py:166-182`, ADR 0016:272): код анализа берётся из привязки протокола, отличающееся значение отвергается без событий; legacy-адаптеры выбираются из жёсткой таблицы `cli.py:176-181` и сверяются с frozen `domain.bind`. При этом прямой `analysis.apply` через `CommandService` вообще не исполняет адаптер (A-04).
7. **Путь модельных предложений ADR 0008 только синтетический; статический тест сохраняет исключения** — подтверждено: `tests/test_pack_universality.py:20` (`LEGACY_EXEMPT = {"agents.py", "experiment_proposals.py", "cli.py"}`), `agents.py:15,26,671-673` импортирует `domains.synthetic_causal` напрямую, `experiment_proposals.py:22` фиксирует `RECIPE_ID = "synthetic_causal_v1"`. Исключение разрешает этим трём модулям импортировать конкретные доменные модули и называть ID пакетов в обход универсальности ADR 0016; тест сканирует только `src/episteme/*.py` и проверяет только `import` и строковые константы, поэтому динамический импорт им не ловится.

## 5. Атаки, которые система корректно отражает

- **Устаревшие решения.** Approval на устаревшем basis отвергается (раздел 4, п. 1); review, принятый на снимке, после которого произошёл конкурентный commit, не сохраняется (`poc14_stale_decision_negative.py`: `ConflictError: research state changed; reload before retry`, `reviews in store: 0`).
- **Replay receipt-backed событий.** Orphan или подделанный `review_assignment` (с self-hashed receipt и утечкой исходника в allowlist) отвергается Graph, export и backup (`poc10`). Receipt-backed handlers не исполняются вне транзакции `CommandService`.
- **Restore.** Существующий (пустой или непустой) destination, лишние члены snapshot, junction вместо `artifacts/sha256`, путеподобный ключ manifest и backup через junction отвергаются; ничего не публикуется (`poc12_restore_negative.py`). Файловые symlinks не проверены (нет привилегии, WinError 1314).
- **Unknown и failed попытки.** `unknown` не переименовывается и не перезапускается автоматически, блокирует settlement batch; failed-попытка сохраняется, расходует бюджет и не может войти в claim (`poc06_unknown_and_failed.py`: `claim evidence must refer to completed runs`, `5th enqueue REJECTED: protocol run budget exhausted`).
- **Pack-путь на самом pack-bound протоколе.** `kernel.claim` и ручной `start_run` отвергаются; `pack.analyse` перезапускает закреплённые хуки и не принимает сфабрикованный отчёт (`domain_packs.py:901-907`); повтор задачи с тем же отчётом отклоняется.
- **Self-review по точной строке.** Reviewer, совпадающий с contributor, отвергается на всех путях допуска (`poc03`), включая авторов hypotheses и связей.
- **Статистические инварианты ядра.** Confirmatory claim на exploratory протоколе и confirmatory split из побайтно идентичных уже просмотренных данных отвергаются; claim обязан цитировать все completed runs протокола; cross-protocol evidence и self-replication тем же ID отвергаются; typed obligation не закрывается одним approval (`test_approval_is_not_a_typed_obligation_transition`).
- **CAS и экспорт.** Каждое in-process чтение CAS проходит `Store.read` с проверкой digest (CasView, выдача reviewer, staging исполнения, materialize, afterlife import); `export_store` остаётся согласованным со снимком при конкурентном commit (`poc05`).
- **`command_id`.** Replay возвращает исторический результат и после других событий; fingerprint покрывает весь context, поэтому результат между studies не утекает. Глобальное пространство `command_id` позволяет занять чужой предсказуемый ID и вызвать conflict, но это документировано (command-api.md:27).

## 6. Какие документированные гарантии выполняются как заявлено

Оценки: **выполняется** — поведение соответствует тексту; **с оговоркой** — верно буквально, но есть обход, о котором текст молчит; **не выполняется** — PoC опровергает утверждение.

| Утверждение (источник) | Оценка | Основание |
| --- | --- | --- |
| Hash chain и triggers защищают от случайной порчи, не от владельца файлов; без внешнего checkpoint перепись и усечение не обнаруживаются (`store.py:1-5`, architecture.md:79, ADR 0001:69, mvp-plan.md:32, recovery.md:76, README.md:209) | выполняется | Ровно заявленная граница (A-13) |
| «append-only события» (README.md:105), «immutable receipt» (architecture.md:75) | с оговоркой | `UPDATE`/`DELETE` блокируются, но `INSERT OR REPLACE` переписывает строки при установленных triggers (A-13) |
| «DB triggers предотвращают UPDATE/DELETE через обычные операции» (architecture.md:77); «UPDATE/DELETE квитанций запрещены обычным SQL» (ADR 0001:85) | не выполняется | A-13, PoC B |
| «Реализовано: immutable event history» (architecture.md:164) | не выполняется как сформулировано | Противоречит architecture.md:79 и A-13 |
| Атомарная запись с expected revision (README.md:167, architecture.md:77) | выполняется | Раздел 5, устаревшие решения |
| Replay команды после потери ответа, conflict при изменении тела (README.md:127, recovery.md:62, command-api.md:27) | с оговоркой | Верно в пределах одной линии store; после restore старого снимка нарушается (A-15) |
| Restore проверяет hashes, inventory, integrity, chain, receipts и Graph (architecture.md:178, recovery.md:46-56) | с оговоркой | Проверки выполняются; схема и triggers не проверяются (A-16); каталог snapshot портится обычными командами (A-21) |
| «Graph проверяет ссылки и байты артефактов» (README.md:192); «Construction verifies one event snapshot» (`graph.py:14`) | с оговоркой | Ложные отказы при конкурентной записи (A-17); роль и независимость reviews не проверяются (A-10) |
| `inspect` = «Verify and inspect existing state» (`cli.py:28`) | с оговоркой | Без replay-проверки receipt-backed событий (A-19) |
| Protocol до RunStarted, фиксированные inputs/code/environment и seed schedule (README.md:170) | с оговоркой | Для primary runs верно; число попыток на seed ограничено только `run_limit`, stopping rule не проверяется (A-07) |
| Запрет повторного объявления просмотренных bytes свежим holdout (README.md:172) | с оговоркой | Верно для идентичных bytes; перекодированная копия проходит (A-09; документировано в ADR 0002:15) |
| Сохранение failed/cancelled attempts и gates на полноту всех результатов (README.md:173) | с оговоркой | Попытки сохраняются, но paper скрывает их метрики (A-07), а чужие попытки отсутствуют в слепом bundle и paper (A-05); против владельца файлов и программы исполнителя не действует (A-12, A-13) |
| Отклонение self-review/self-replication по ID (README.md:174) | с оговоркой | Работает на путях допуска при точном совпадении строки; read side не перепроверяет (A-10); строки не нормализуются (A-22) |
| Отрицательное мнение не отменяется одобрением другого reviewer или сменой basis (README.md:147) | не выполняется | Верно для одного ID claim; повторный claim обходит veto и obligations (A-01) |
| Veto и блокировка premature paper (README.md:176; ADR 0009:37); «другие открытые obligations … блокируют потомка» (ADR 0010:13, architecture.md:91) | не выполняется | A-01, A-02; при caller-declared ID — также A-06 |
| «Failed foreign attempts остаются контекстом review» (ADR 0002:27) | не выполняется | Есть в basis typed claim, но не в слепом bundle и paper (A-05) |
| Слепой initial review: прежние reviews и исходники не входят в проекцию; read isolation не обеспечена (ADR 0011:13,19; ADR 0012:19) | выполняется | Manifest не содержит прежних verdicts; provider читает их из Store, как и заявлено (раздел 4, п. 3); побочный эффект — A-14 |
| ID задаёт доверенный caller; разные ID и hashes не доказывают независимость (`kernel.py:1-4`, README.md:209, architecture.md:130, ADR 0010:11, ADR 0013:18) | выполняется | Документация честна; A-06, A-08 и A-22 показывают практические последствия |
| Demo останавливается до review и не фабрикует approval (README.md:153) | с оговоркой | Сам demo не фабрикует; CLI позволяет одной командой записать approval для demo-вывода и собрать paper (A-06) |
| Адаптер `synthetic_causal_v1` возвращает только `inconclusive`/`exploratory` (ADR 0013:16) | не выполняется для допуска | Свойство адаптера; `analysis.apply` принимает любой proposal (A-04) |
| На pack-bound protocol legacy-команды не обходят потолок (ADR 0016:262) | не выполняется | Обход через amendment (A-03) |
| Trust boundary пакетов: malicious pack вне scope, хуки in-process (ADR 0016:61) | выполняется | Документация честна (A-11) |
| Статическая проверка — не граница безопасности; вызовы `__import__/eval/exec/compile` «отвергаются» (ADR 0016:247) | с оговоркой | Отвергаются только вызовы по имени (A-11) |
| Host inputs скрыты от hooks анализа, но не секретны (ADR 0016:265; README.md:35) | с оговоркой | Скрыты только от чтения через `CasView`; хук читает их через Store (A-11) |
| `pack.analyse` допускает только отчёт, побайтно равный пересчёту закреплённым кодом (ADR 0016:260) | выполняется | Код `domain_packs.py:901-907`; подмена возможна только кодом в том же процессе (A-11) |
| Исполнение — trusted local, без sandbox, программа может читать файлы, сеть, environment (ADR 0005:9, ADR 0016:16, README.md:98, architecture.md:150) | выполняется | A-12; «отдельный рабочий каталог» (ADR 0005:7) вложен в корень хранилища, и это не сказано |
| `unknown` не переименовывается и не перезапускается; unknown блокирует settlement (ADR 0005:45, ADR 0006:35) | выполняется | Раздел 5 |
| Протокол с прежними runs нельзя повторно поставить в batch (ADR 0006:13) | выполняется, но только в пределах протокола | Перерегистрация протокола обходит это ограничение (A-05) |
| Универсальность ядра для пакетов (ADR 0016, критерий 1) | с оговоркой | Исключения `LEGACY_EXEMPT` (раздел 4, п. 7) |

## 7. Не воспроизведено и вне охвата

- Подделка `completion.json` из другого job для перевода `unknown` в `completed`: reconcile проверяет внутреннюю согласованность записи, но не то, что её написал именно worker (attestation не реализована, ADR 0005:89). Гипотеза не проверялась запуском.
- Выход из Windows Job Object не исследовался.
- Обход проверки пересечения `pack capture` через junction или префикс `\\?\` не проверялся; путь `afterlife import --actor` не тестировался.
- Файловые symlinks не тестировались (нет привилегии на этой учётной записи); вместо них проверены junctions.
- Пути с провайдером модели (`codex_provider.py`, команды `agent`) исключены ограничениями задания.
- Изменения после `da6aa2a`, в том числе параллельная оптимизация проверки receipts, не аудировались.

## 8. Рекомендуемый порядок исправлений

1. **Семантика veto, obligations и резолюций (A-01, A-02).** Вычислять их по семейству claim (протокол и линия `parent`/`replan_followup` или пересекающиеся runs), привязать резолюцию к её claim, проверить `dp["claim"]` в paper lineage; добавить регрессионные тесты. Правка небольшая, эффект максимальный, и без неё не поможет даже будущая аутентификация.
2. **Происхождение и потолок claims (A-03, A-04).** Наследовать pack binding по линии `parent` и запретить non-pack amendments pack-bound протоколов; в `analysis.apply` пересчитывать proposal адаптером, как это уже делает `pack.analyse`.
3. **Read-side валидация reviews и закрытие legacy-пути (A-10, A-06, A-22).** Засчитывать только reviews с ролью reviewer, автором вне contributors и receipt review-команды; paper — только по submissions, привязанным к assignment; убрать или перевести CLI `review`; нормализовать actor IDs; отказывать в paper для synthetic demo без явного флага.
4. **Полное раскрытие попыток (A-05, A-07, затем A-08, A-09).** Чужие попытки и метрики failed-попыток — в manifest и paper; сигнал gate о повторе seed; пометка provenance outputs (managed/caller-declared); tripwires для скопированных реанализов и confirmatory runs на просмотренных bytes.
5. **Изоляция исполнения и хуков (A-12, A-11, A-23).** Allowlist окружения, workspace вне корня хранилища, сверка head и `sqlite_master` до и после запуска; хуки пакетов в subprocess (зарезервированный `hook_isolation`); честные формулировки ADR 0016:247/265.
6. **Жёсткость хранилища (A-13, A-16, A-18, A-19, затем A-15).** `BEFORE INSERT` triggers, проверка схемы при открытии и restore, канонический текст payload, replay в `inspect`; затем внешний checkpoint head hash и lineage epoch, которые закрывают и откат через restore.
7. **Надёжность и UX (A-17, A-20, A-21, A-14).** Одна read-транзакция в Graph, карантин повреждённых CAS-файлов, защита snapshot и маркер незавершённого restore, политика v2 слепого контекста для связанных claims.
8. **Документация.** Привести к фактическому поведению README.md:105/147/172-176, architecture.md:77/164, ADR 0001:63/85, ADR 0013:16, ADR 0016:247/262/265 и отметить в recovery.md ограничение идемпотентности после restore.

## 9. Статус исправлений

Раздел обновляется по мере исправлений; разделы 1–8 описывают снимок `da6aa2a` и не меняются. Исправления семантики review, claims и paper ведёт [ADR 0018](decisions/0018-claim-families-and-review-admission.md), принятый 4 октября 2026 года координирующим агентом по делегированию пользователя. «Исправлено» означает: PoC находки стал регрессионным тестом, который падает на коде до исправления и проходит после него; номера прогонов — в [validation.md](validation.md). Шаг ADR указан по его разделу 9.

Последняя проверка: 4 октября 2026, шаг 3 ADR 0018.

| ID | Статус | Где исправляется |
| --- | --- | --- |
| A-01 | **исправлено** (шаг 2): veto и obligations действуют на семейство claim; PoC — `test_resubmitted_claim_inherits_family_veto_and_obligations` (typed и legacy reject), перерегистрация на тех же bytes — `test_reregistered_protocol_on_same_bytes_joins_the_family` | ADR 0018, шаг 2 |
| A-02 | **исправлено** (шаг 2): resolution засчитывается только для своего claim; PoC — `test_resolution_applies_only_to_the_claim_it_evaluated`, lineage потомка — `test_descendant_protocol_paper_needs_its_own_resolution` | ADR 0018, шаг 2 |
| A-03 | **исправлено** (шаг 3): привязка пакета управляет линией `parent`; PoC — `test_amendment_of_pack_bound_protocol_is_rejected`, путь через `followup.apply` — `test_followup_of_pack_bound_claim_is_rejected`, перерегистрация закреплённых bytes — `test_legacy_root_cannot_reuse_pack_pinned_bytes`, история эпохи `da6aa2a` — `test_stray_claim_on_pack_lineage_fails_gate` | ADR 0018, шаг 3 |
| A-04 | не исправлено | ADR 0018, шаг 4 |
| A-05 | не исправлено | ADR 0018, шаг 8 |
| A-06 | не исправлено | ADR 0018, шаг 7 |
| A-07 | не исправлено | ADR 0018, шаг 8 |
| A-08 | не исправлено | ADR 0018, шаг 9; частично остаётся ограничением (§6 ADR) |
| A-09 | не исправлено | ADR 0018, шаг 9; частично остаётся ограничением (§6 ADR) |
| A-10 | не исправлено | ADR 0018, шаг 7 |
| A-11 | вне ADR 0018 | будущий ADR профиля исполнения; ADR 0017 код пакетов не менял |
| A-12 | вне ADR 0018 | будущий ADR профиля исполнения; ADR 0017 код исполнения не менял |
| A-13 | вне ADR 0018; **перепроверить** | ADR 0017 переписал проверку цепочки в `store.py`; triggers и `INSERT OR REPLACE` он не менял |
| A-14 | не исправлено | ADR 0018, шаги 6 и 8 |
| A-15 | вне ADR 0018 | ADR 0017 `recovery.py` не менял |
| A-16 | вне ADR 0018; **перепроверить** | ADR 0017 менял `store.py`; открытие Store и triggers, судя по diff, прежние |
| A-17 | **не исправлено ADR 0017** (проверено) | см. ниже |
| A-18 | вне ADR 0018; **перепроверить** | ADR 0017 переписал разбор строк событий в `store.py` |
| A-19 | вне ADR 0018; **перепроверить** | ADR 0017 переписал проверку receipts в `store.py`; `inspect` и `gate` в `cli.py` он оборачивал только в read scope |
| A-20 | вне ADR 0018; перепроверить | ADR 0017 менял `Store.read` (memo в read scope); `put` прежний |
| A-21 | вне ADR 0018 | изменения ADR 0017 в `cli.py` механические (read scope) |
| A-22 | не исправлено | ADR 0018, шаг 5 |
| A-23 | вне ADR 0018 | будущий ADR профиля исполнения; изменения ADR 0017 в `cli.py` поведение `pack describe`/`pack verify` не меняют |

«Перепроверить» значит: ADR 0017 менял код, через который проходит PoC находки, а сам PoC после него не повторялся. Находка считается открытой, пока PoC не повторён.

**A-17 после ADR 0017.** Проверено на `73281b5` детерминированным чередованием (`.research/adr0018-a17-check/a17_check.py`, вне репозитория): второе соединение записывает команду между `store.events()` и `_verified_receipts(history)` первого. `_verified_receipts` сверяет каждую строку receipts со старым снимком и отказывает (`command receipt corrupt: …: invalid command receipt event range`); так же отказывает построение Graph на этом снимке. Находка воспроизводится. Согласованный префикс даёт `store.receipts()` с фильтром `after_revision <= len(history)`; этим приёмом пользуются все replay-индексы и проекции ADR 0018.

## Приложение A. Helper `Lab` (`poc_common.py`)

```python
"""Shared fixture for review-class PoCs (audit of da6aa2a snapshot)."""
import tempfile
from pathlib import Path
from uuid import uuid4

import episteme
from episteme.commands import CommandService
from episteme.store import Store

print("episteme from:", episteme.__file__)


class Lab:
    def __init__(self, prefix="poc-"):
        self.root = Path(tempfile.mkdtemp(prefix=prefix))
        self.store = Store(self.root)
        self.svc = CommandService(self.store)

    def cmd(self, actor, role, action, study="study-1", **payload):
        envelope = dict(context=dict(command_id=f"{action}-{uuid4().hex}",
                                     expected_revision=len(self.store.events()), actor=actor,
                                     role=role, study_id=study, correlation_id="poc",
                                     causation_id=None),
                        request=dict(version=1, action=action, payload=payload))
        return self.svc.execute(envelope)

    def basic_claim_world(self, *, outcome="supports", statement="Effect X exists",
                          metric_value=0.0):
        """Two hypotheses, a protocol, a primary run + reanalysis, and claim A."""
        s = self.store
        self.scope = {"population": "synthetic"}
        hyp = [self.cmd("planner-1", "planner", "kernel.hypothesis", statement=n, prediction=n,
                        falsifier="opposite", scope=self.scope) for n in ("signal", "null")]
        self.code, self.recode, self.env, self.raw = (
            s.put(b) for b in (b"primary code", b"reanalysis code", b"env", b"0\n"))
        self.protocol = self.cmd("planner-1", "planner", "kernel.preregister", hypotheses=hyp,
            scope=self.scope, design="d", metric="mean", analysis_plan="a",
            stopping_rule="one seed", seeds=[7], run_limit=4, implementation=self.code,
            environment=self.env, data=self.raw, replication_tolerance=0.0)
        self.outputs = dict(raw_data=self.raw, metrics=s.put_json({"mean": metric_value}),
                            log=s.put(b"log"))
        self.primary = self.cmd("exec-1", "executor", "kernel.start_run", protocol=self.protocol,
            seed=7, implementation=self.code, environment=self.env, command=["x"])
        self.cmd("exec-1", "executor", "kernel.finish_run", run=self.primary,
                 status="completed", outputs=self.outputs)
        self.replica = self.cmd("repl-1", "replicator", "kernel.start_run",
            protocol=self.protocol, seed=7, implementation=self.recode, environment=self.env,
            command=["x"], replicate_of=self.primary)
        self.cmd("repl-1", "replicator", "kernel.finish_run", run=self.replica,
                 status="completed", outputs=self.outputs)
        return self.new_claim(statement=statement, outcome=outcome)

    def new_claim(self, *, statement="Effect X exists", outcome="supports"):
        return self.cmd("analyst-1", "analyst", "kernel.claim", protocol=self.protocol,
                        statement=statement, scope=self.scope,
                        evidence=[self.primary, self.replica], limitations=["synthetic"],
                        outcome=outcome)

    def basis(self, claim):
        from episteme.kernel import Actor, Kernel
        return Kernel(self.store, Actor("obs", "observer")).gate(claim)["basis_hash"]

    def next_action(self, claim):
        from episteme.kernel import Actor, Kernel
        return Kernel(self.store, Actor("obs", "observer")).next_action(claim)
```

## Приложение B. Перечень PoC

PoC хранятся вне репозитория, во временном рабочем каталоге аудита (`%TEMP%\episteme-audit-work`); код, нужный для воспроизведения, приведён выше.

| Каталог | Файлы | Находки |
| --- | --- | --- |
| `review` | `poc_common.py`, `poc_r1_duplicate_claim.py`, `poc_r2_cli_impersonated_withdrawal.py`, `poc_r3_stale_approval.py`, `poc_r4_resolution_transfer.py`, `poc_r5_confirmatory_relabel.py`, `poc_r6_retry_and_copied_reanalysis.py`, `poc_r7_file_drawer.py`, `poc_r8_blind_vs_acknowledge.py`, `poc_r9_raw_review_event.py`, `poc_r10_provider_reads_store.py`, `poc_r11_analysis_apply_forged_proposal.py` | A-01, A-06, раздел 4 п. 1, A-02, A-09, A-07/A-08, A-05, A-14, A-10, раздел 4 п. 3, A-04 |
| `storage` | `common.py`, `poc01_tail_truncation.py`, `poc01b_midhistory_rechain.py`, `poc02_insert_or_replace.py`, `poc03_neutered_triggers.py`, `poc04_rollback_fork.py`, `poc05_graph_receipt_race.py`, `poc06_cas_unrepairable.py`, `poc07_snapshot_mutated.py`, `poc08_crash_leftovers.py`, `poc09_payload_text_malleable.py`, `poc10_orphan_forged_receipt.py`, `poc11_executor_program_tamper.py`, `poc12_restore_negative.py`, `poc13_command_id_scope.py`, `poc14_stale_decision_negative.py`, `fix_check.py` | A-13, A-16, A-15, A-17, A-20, A-21, A-18, A-19, A-12, раздел 5 |
| `packs-runner` | `harness.py`, `malpack/malicious_pack_v1/__init__.py`, `poc01_import_bypass.py`, `poc02_executor_env_escape.py`, `poc03_actor_overlap.py`, `poc04_readonly_exec.py`, `poc05_ceiling_subversion.py`, `poc06_unknown_and_failed.py`, `poc07_cross_protocol_rerun.py`, `poc08_amendment_escape.py` | A-11, A-12, A-22, A-23, раздел 5, A-05, A-03 |
