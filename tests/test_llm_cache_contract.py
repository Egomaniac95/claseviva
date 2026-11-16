from src import post_procesar


def test_cache_distinguishes_output_budget_roles_and_boundaries(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("CLASE_POST_CACHE", "1")
    monkeypatch.setenv("CLASE_POST_LLM", "1")
    monkeypatch.setattr(post_procesar, "preguntar", lambda messages, **kwargs: calls.append(messages) or "Texto completo")
    first = [{"role": "system", "content": "A"}, {"role": "user", "content": "B"}]
    def ask(messages, budget=1000):
        return post_procesar.preguntar_cacheado(tmp_path, messages, budget, "test", "v1")
    assert ask(first) == ask(first)
    assert len(calls) == 1
    ask(first, 2000)
    ask([{ "role": "user", "content": "A"}, {"role": "system", "content": "B"}])
    ask([{ "role": "user", "content": "A\n\nB"}])
    assert len(calls) == 4
