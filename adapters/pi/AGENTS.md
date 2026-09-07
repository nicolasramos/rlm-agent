# RLM Usage Rules (cuándo usar el sistema RLM)

RLM = kernel persistente (`ipython`), context lake (`rlm_store`/`rlm_get`/`rlm_search`/`rlm_find`/`rlm_stats`/`rlm_forget`), subagentes background (`rlm`/`rlm_list`/`rlm_result`), snapshot/restore (`rlm_snapshot`/`rlm_restore`).

## Regla de decisión: ¿RLM o flujo normal?

Usa RLM cuando **dos o más** de estas condiciones se cumplen:

- **Datos grandes**: el dato supera ~10K chars o ~2.5K tokens (logs largos, datasets, salidas de herramientas, transcripciones).
- **Re-uso**: vas a consultar el MISMO dato más de una vez en la tarea (parsear → filtrar → transformar → resumir).
- **Estado multi-paso**: la tarea tiene 3+ pasos que comparten estado intermedio (variables, resultados parciales).
- **Contexto largo**: la tarea va a generar mucho texto intermedio que no necesitas ver completo.

## Qué herramienta usar

| Situación | Herramienta | Ejemplo |
|---|---|---|
| Estado entre pasos de UNA tarea | `ipython` (kernel) | Cargar un log, filtrarlo, contar, resumir — todo en variables del kernel |
| Dato grande que se re-consulta o sobrevive a la sesión | `rlm_store` + `rlm_search`/`rlm_find` | Log de 5.000 líneas: guardar en lake, buscar la aguja con regex |
| Trabajo independiente que corre mientras sigo | `rlm` + `rlm_result` | Lanzar un análisis en background y seguir con otra cosa |
| Antes de compactación / sesión larga | `rlm_snapshot` | Persistir el estado del kernel a disco |
| Tras compactación / reinicio | `rlm_restore` | Recuperar variables del snapshot |

## Anti-patrones (NO usar RLM)

- Tareas de 1-2 llamadas (un grep, un read_file, una pregunta simple) → flujo normal.
- Datos que se usan UNA vez y caben en contexto → flujo normal.
- El lake NO es un cajón de basura: solo guardar datos que se re-consulten. `rlm_forget` para limpiar.
- No lanzar subagentes para trabajo que yo mismo hago en 2 llamadas.

## Gotchas verificados

- `ipython` no captura `print()` — devolver el valor como expresión final (`rlm_test_var + 1` → `43`).
- `%%bash` ejecuta (exit code) pero no captura stdout.
- `rlm_result` devuelve `{status, summary, error}` cuando el child termina (SUCCEEDED/FAILED); mientras corre, `{status}`.
- El kernel es por sesión: `%cd` persiste, variables persisten entre llamadas de la MISMA sesión.
