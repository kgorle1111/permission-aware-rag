from mutants import PostFilterTopK

from evals.kit.reference_adapter import ReferenceAdapter


def factory():
    return ReferenceAdapter(PostFilterTopK)
