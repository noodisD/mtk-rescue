from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .mtk import MtkClient


RecipeRunner = Callable[[MtkClient], Iterator[str]]


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    description: str
    writes_device: bool
    runner: RecipeRunner


def _printgpt(mtk: MtkClient) -> Iterator[str]:
    yield from mtk.stream("printgpt")


# TODO(mvp+1):
#   def _restore_gpt(mtk): ...
#   def _flash_preloader(mtk, src: Path): ...     # w preloader --parttype=boot1/boot2
#   def _seccfg_lock(mtk): ...                    # da seccfg lock
#   def _seccfg_unlock(mtk): ...                  # da seccfg unlock
#   def _wipe_userdata(mtk): ...                  # e userdata,metadata,md_udc,cache


RECIPES: dict[str, Recipe] = {
    "diag.printgpt": Recipe(
        id="diag.printgpt",
        title="List GPT partitions",
        description="Read the GPT from device and display every partition entry. Read-only.",
        writes_device=False,
        runner=_printgpt,
    ),
}
