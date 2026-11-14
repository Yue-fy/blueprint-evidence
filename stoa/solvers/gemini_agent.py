"""Registers `gemini_agent` (G1): Gemini plan-and-execute over the pipeline's atomic cascade. Needs $GEMINI_API_KEY.

Google's no-abuse rule: small sets only (the eval hides the key on runs of > 200 questions; stay far below)."""
from stoa.solvers import register
from stoa.solvers.base import BaseSolver


@register("gemini_agent")
class GeminiAgentSolver(BaseSolver):
    def __init__(self, model: str | None = None, image: int | str = 1, **kwargs):
        super().__init__(**kwargs)
        from stoa.agent.gemini import PLANNER, GeminiAgent
        from stoa.pipeline import Pipeline

        self._pipe = Pipeline(agent=0)
        self._agent = GeminiAgent(ask_fn=lambda p, q: self._pipe._solve_atomic(p, q, allow_agent=False),
                                  model=model or PLANNER, image=bool(int(image)))

    def solve(self, path, question):
        return self._agent.solve(path, question)
