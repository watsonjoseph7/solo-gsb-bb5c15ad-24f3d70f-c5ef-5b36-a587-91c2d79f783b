"""请求/响应 Pydantic 模型（Pydantic v2）。"""
from typing import List, Optional

from pydantic import BaseModel, Field


def normalize_allergens(value: Optional[str]) -> List[str]:
    """把逗号/顿号分隔的过敏原字符串规范化为去重、去空的列表，保持顺序。"""
    if not value:
        return []
    parts = value.replace("、", ",").replace("，", ",").split(",")
    result: List[str] = []
    for p in parts:
        item = p.strip()
        if item and item not in result:
            result.append(item)
    return result


class IngredientIn(BaseModel):
    code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    allergens: str = ""  # 逗号分隔，如 "乳, 大豆"
    note: str = ""


class IngredientOut(IngredientIn):
    id: int
    allergen_list: List[str]


class RecipeItemIn(BaseModel):
    ingredient_code: str
    quantity: Optional[float] = None
    unit: str = ""


class RecipeIn(BaseModel):
    code: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    note: str = ""
    items: List[RecipeItemIn] = []


class RecipeItemOut(BaseModel):
    ingredient_code: str
    ingredient_name: str
    quantity: Optional[float]
    unit: str


class RecipeOut(BaseModel):
    id: int
    code: str
    version: str
    name: str
    note: str
    created_at: str
    items: List[RecipeItemOut]


class SubstitutionIn(BaseModel):
    source_code: str = Field(..., min_length=1)
    target_code: str = Field(..., min_length=1)
    note: str = ""


class SubstitutionOut(SubstitutionIn):
    id: int
    source_name: str
    target_name: str
    source_allergens: List[str]
    target_allergens: List[str]


class SafePlanIn(BaseModel):
    avoid_allergens: List[str] = Field(default_factory=list, description="需要避开的过敏原列表")


class PlanSelectionItemIn(BaseModel):
    ingredient_code: str = Field(..., min_length=1, description="配方配料（原料）代码")
    selected_code: str = Field(..., min_length=1, description="所选终点原料代码")
    path_codes: List[str] = Field(
        default_factory=list,
        description="生成结果中的替代路径节点代码序列（根→终点），用于精确匹配候选",
    )


class ConfirmPlanIn(BaseModel):
    avoid_allergens: List[str] = Field(default_factory=list, description="生成方案时勾选的需避开过敏原")
    selections: List[PlanSelectionItemIn] = Field(default_factory=list, description="用户选定的一组方案")


class ExportBundle(BaseModel):
    ingredients: List[IngredientIn] = []
    recipes: List[RecipeIn] = []
    substitutions: List[SubstitutionIn] = []


class ImportResult(BaseModel):
    ingredients: int = 0
    recipes: int = 0
    substitutions: int = 0
    errors: List[str] = []
