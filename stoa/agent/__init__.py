"""G1: the general agent for unseen question kinds (Gemini planner) and the Nemotron verifier.

- `stoa.agent.gemini.GeminiAgent`   plan-and-execute: Gemini 3.1 Pro writes the whole plan in ONE call, our local
                                    tools run it (no LLM in the loop), one more Gemini call reads the results and
                                    answers. Numbers come from tools; confidence from the tools (router Agent rules).
- `stoa.agent.verify.verify`        hosted Nemotron looks at the evidence crop + the reasoning and agrees / disagrees;
                                    the confidence moves accordingly (never the value).
- `stoa.agent.escalate.escalate`    the pipeline's last step for unsure answers (solver args gemini=1 / verify=1,
                                    both OFF by default: Google's no-abuse rule and NVIDIA credits; demo / small sets).
"""
