/* Движок сценариев отказов без DOM: зеркало coverage/scenario.py.
   Нужен автономному map.html (там нет сервера) и для ручной проверки. Логика обязана совпадать с Python:
   тест tests/test_scenario_js.py сравнивает результаты через node (если он есть). */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CoverageScenario = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const CATEGORIES = ["owner", "permit", "technical", "other"];
  const MAX_REQUIRED = 2;
  const MAX_REASON = 200;
  const DEFAULT_TOP_N = 10;

  class ScenarioError extends Error {}

  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  const sortedArr = (it) => [...it].sort(cmp);
  const diff = (a, b) => new Set([...a].filter((x) => !b.has(x)));
  const inter = (a, b) => new Set([...a].filter((x) => b.has(x)));
  const union = (a, b) => new Set([...a, ...b]);

  /** id допустимых по ТЗ кандидата -> Set красных клеток в радиусе покрытия. */
  function coversOf(data) {
    const out = new Map();
    data.candidates.forEach((c) => {
      if (c.admissible) out.set(c.id, new Set(c.covers || []));
    });
    return out;
  }

  function optimumOf(data, covers) {
    const pair = sortedArr(data.compare ? data.compare.brute_force.pair : data.result.best_candidates);
    const cells = pair.length === 2 && pair.every((p) => covers.has(p)) ? union(covers.get(pair[0]), covers.get(pair[1])) : new Set();
    return { pair, cells };
  }

  function normalize(constraints) {
    return {
      excluded: (constraints.excluded || []).map((r) => ({ id: r.id, reason: r.reason || "", category: r.category || "other" })),
      required: [...(constraints.required || [])],
    };
  }

  function validate(data, constraints, towerExclusion) {
    const c = normalize(constraints);
    const info = new Map(data.candidates.map((x) => [x.id, x]));
    const excl = towerExclusion === undefined && data.meta ? data.meta.tower_exclusion_m : towerExclusion;
    const errors = [];
    c.excluded.forEach((r) => {
      if (!info.has(r.id)) errors.push(`неизвестная площадка в отказах: ${r.id}`);
      if (!CATEGORIES.includes(r.category)) errors.push(`${r.id}: неизвестная категория отказа «${r.category}» (допустимы: ${CATEGORIES.join(", ")})`);
      if (r.reason.length > MAX_REASON) errors.push(`${r.id}: причина длиннее ${MAX_REASON} символов`);
    });
    if (new Set(c.required).size !== c.required.length) errors.push("одна и та же площадка зафиксирована дважды");
    if (c.required.length > MAX_REQUIRED) errors.push(`можно зафиксировать не больше ${MAX_REQUIRED} площадок, передано ${c.required.length}`);
    const exIds = new Set(c.excluded.map((r) => r.id));
    c.required.forEach((id) => {
      const x = info.get(id);
      if (!x) {
        errors.push(`неизвестная площадка в фиксации: ${id}`);
        return;
      }
      if (exIds.has(id)) errors.push(`${id} одновременно в отказах и в фиксации`);
      if (!x.admissible) {
        if (x.nearest_tower != null && x.nearest_tower_m != null && excl != null) {
          errors.push(`${id} нельзя зафиксировать: ${Math.round(x.nearest_tower_m)} м до ${x.nearest_tower} (< ${excl} м)`);
        } else {
          errors.push(`${id} нельзя зафиксировать: ${x.reason}`);
        }
      }
    });
    return errors;
  }

  function evaluatePair(covers, a, b, optCells) {
    [a, b].forEach((id) => {
      if (!covers.has(id)) throw new ScenarioError(`площадка ${id} неизвестна или недопустима по ТЗ`);
    });
    if (a === b) throw new ScenarioError("нужны две разные площадки");
    const [p, q] = sortedArr([a, b]);
    const ca = covers.get(p);
    const cb = covers.get(q);
    const un = union(ca, cb);
    const opt = optCells || un;
    return {
      pair: [p, q],
      covered: un.size,
      covered_ids: sortedArr(un),
      only_a: diff(ca, cb).size,
      only_b: diff(cb, ca).size,
      overlap: inter(ca, cb).size,
      delta_vs_optimum: un.size - opt.size,
      lost_vs_optimum: sortedArr(diff(opt, un)),
      gained_vs_optimum: sortedArr(diff(un, opt)),
    };
  }

  function pairIds(covers, c) {
    const exIds = new Set(c.excluded.map((r) => r.id));
    const allowed = sortedArr([...covers.keys()].filter((id) => !exIds.has(id)));
    const req = sortedArr(c.required);
    if (req.length === 2) return req.every((r) => allowed.includes(r)) ? [req] : [];
    if (req.length === 1) {
      const r = req[0];
      return allowed.includes(r) ? allowed.filter((x) => x !== r).map((x) => sortedArr([r, x])) : [];
    }
    const out = [];
    for (let i = 0; i < allowed.length; i++) for (let j = i + 1; j < allowed.length; j++) out.push([allowed[i], allowed[j]]);
    return out;
  }

  /** Все пары: покрытие по убыванию, затем пара лексикографически. */
  function enumeratePairs(covers, constraints, optCells) {
    const c = normalize(constraints);
    const opts = pairIds(covers, c).map(([a, b]) => evaluatePair(covers, a, b, optCells));
    opts.sort((x, y) => y.covered - x.covered || cmp(x.pair[0], y.pair[0]) || cmp(x.pair[1], y.pair[1]));
    return opts;
  }

  function bestCoveredExcluding(covers, excluded) {
    const ids = sortedArr([...covers.keys()].filter((id) => !excluded.includes(id)));
    let best = 0;
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) best = Math.max(best, union(covers.get(ids[i]), covers.get(ids[j])).size);
    }
    return best;
  }

  function impactOfRefusals(covers, optimumCovered) {
    const out = {};
    sortedArr(covers.keys()).forEach((id) => {
      out[id] = bestCoveredExcluding(covers, [id]) - optimumCovered;
    });
    return out;
  }

  function replacements(covers, c, optimum) {
    const exIds = new Set(c.excluded.map((r) => r.id));
    const refused = optimum.pair.filter((id) => exIds.has(id));
    if (refused.length !== 1) return [];
    const x = refused[0];
    const y = optimum.pair.find((id) => id !== x);
    const rebuild = enumeratePairs(covers, { excluded: c.excluded, required: [] }, optimum.cells)[0];
    const keep = enumeratePairs(covers, { excluded: c.excluded, required: [y] }, optimum.cells)[0];
    const keepWins = !!keep && (!rebuild || keep.covered >= rebuild.covered);
    const out = [];
    if (keep) out.push({ kind: "keep_partner", kept: y, replaced: x, pair: keep.pair, covered: keep.covered, delta: keep.delta_vs_optimum, recommended: keepWins });
    if (rebuild) out.push({ kind: "rebuild", pair: rebuild.pair, covered: rebuild.covered, delta: rebuild.delta_vs_optimum, recommended: !keepWins });
    return out;
  }

  const brief = (o, withIds) => {
    const d = { pair: o.pair, covered: o.covered, delta_vs_optimum: o.delta_vs_optimum, overlap: o.overlap, only_a: o.only_a, only_b: o.only_b };
    if (withIds) {
      d.covered_ids = o.covered_ids;
      d.lost_vs_optimum = o.lost_vs_optimum;
      d.gained_vs_optimum = o.gained_vs_optimum;
    }
    return d;
  };

  /** Тот же ответ, что POST /api/scenario. Бросает ScenarioError при некорректных ограничениях. */
  function runScenario(data, constraints, topN) {
    const n = topN === undefined ? DEFAULT_TOP_N : topN;
    if (!(n >= 1 && n <= 50)) throw new ScenarioError("top_n должен быть от 1 до 50");
    const errors = validate(data, constraints);
    if (errors.length) throw new ScenarioError(errors.join("; "));
    const c = normalize(constraints);
    const covers = coversOf(data);
    const optimum = optimumOf(data, covers);
    const optPair = optimum.pair.length === 2 ? evaluatePair(covers, optimum.pair[0], optimum.pair[1], optimum.cells) : null;
    const optOut = optPair
      ? brief(optPair, true)
      : { pair: optimum.pair, covered: optimum.cells.size, delta_vs_optimum: 0, overlap: 0, only_a: 0, only_b: 0, covered_ids: sortedArr(optimum.cells), lost_vs_optimum: [], gained_vs_optimum: [] };
    const impact = impactOfRefusals(covers, optOut.covered);
    const ranked = enumeratePairs(covers, c, optimum.cells);
    const exIds = new Set(c.excluded.map((r) => r.id));
    const available = [...covers.keys()].filter((id) => !exIds.has(id));
    const reps = optPair ? replacements(covers, c, { pair: optimum.pair, cells: optimum.cells }) : [];
    const base = { constraints: c, optimum: optOut, candidate_impact: impact, result_json_unchanged: true };
    if (!ranked.length) {
      const message =
        available.length < 2
          ? `сценарий невыполним: после отказов доступно площадок: ${available.length}, нужно не меньше двух`
          : "сценарий невыполним: нет пары, удовлетворяющей фиксации";
      return { feasible: false, message, ...base, best: null, ties: [], alternatives: [], replacements: reps };
    }
    const best = ranked[0];
    return {
      feasible: true,
      message: null,
      ...base,
      best: brief(best, true),
      ties: ranked.filter((o) => o.covered === best.covered).map((o) => ({ pair: o.pair, covered: o.covered })),
      alternatives: ranked.slice(0, n).map((o) => brief(o, false)),
      replacements: reps,
    };
  }

  /** Тот же ответ, что POST /api/scenario/evaluate. */
  function evaluateManual(data, a, b, constraints) {
    const c = normalize(constraints);
    const covers = coversOf(data);
    const optimum = optimumOf(data, covers);
    const o = evaluatePair(covers, a, b, optimum.cells);
    const exIds = new Set(c.excluded.map((r) => r.id));
    const violates = o.pair.some((id) => exIds.has(id)) || !c.required.every((id) => o.pair.includes(id));
    const ranked = enumeratePairs(covers, c, optimum.cells);
    return { ...brief(o, true), violates_constraints: violates, delta_vs_best: ranked.length ? o.covered - ranked[0].covered : null };
  }

  return { CATEGORIES, ScenarioError, validate, evaluatePair, enumeratePairs, impactOfRefusals, runScenario, evaluateManual, coversOf, optimumOf };
});
