from .runner import search_kroger, KrogerResult

# Legacy alias so any code still using run_kroger_agent doesn't break during transition
run_kroger_agent = search_kroger
KrogerAgentResult = KrogerResult

__all__ = ["search_kroger", "KrogerResult", "run_kroger_agent", "KrogerAgentResult"]
