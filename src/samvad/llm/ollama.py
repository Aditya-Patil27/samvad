# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Local Ollama backend -- the offline peer.

Not just a cost saving. A different model family gives UNCORRELATED errors,
which is the whole basis of measurement 5. Two instances of one model
agreeing is worth almost nothing; they share training priors and wave through
exactly the mistakes they would have made themselves.
"""


class OllamaBackend:
    async def complete(self, prompt):
        raise NotImplementedError
