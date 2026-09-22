"""内置示例数据：一条黄油 <-> 人造黄油循环，两版曲奇配方。"""
from . import crud
from .schemas import IngredientIn, RecipeIn, RecipeItemIn, SubstitutionIn

INGREDIENTS = [
    IngredientIn(code="BUTTER", name="黄油", allergens="乳", note="动物油脂"),
    IngredientIn(code="MARGARINE", name="人造黄油", allergens="大豆", note="植物油脂，可能含乳成分"),
    IngredientIn(code="GHEE", name="澄清黄油", allergens="乳", note="高温黄油"),
    IngredientIn(code="WHEAT_FLOUR", name="小麦粉", allergens="小麦（麸质）", note="中筋面粉"),
    IngredientIn(code="RICE_FLOUR", name="米粉", allergens="", note="无麸质替代粉"),
    IngredientIn(code="ALMOND_FLOUR", name="杏仁粉", allergens="坚果（杏仁）", note="坚果粉"),
    IngredientIn(code="EGG", name="鸡蛋", allergens="蛋", note="鲜鸡蛋"),
    IngredientIn(code="APPLESAUCE", name="苹果泥", allergens="", note="无蛋替代，约 1/4 杯替 1 蛋"),
    IngredientIn(code="SOY_SAUCE", name="酱油", allergens="小麦（麸质）,大豆", note="酿造酱油"),
    IngredientIn(code="TAMARI", name="溜酱油（Tamari）", allergens="大豆", note="通常无麸质，仍需核对标签"),
    IngredientIn(code="MILK", name="牛奶", allergens="乳", note="全脂奶"),
    IngredientIn(code="OAT_MILK", name="燕麦奶", allergens="可能含麸质（燕麦交叉污染）", note="植物奶"),
]

SUBSTITUTIONS = [
    # 双向边 => 循环
    SubstitutionIn(source_code="BUTTER", target_code="MARGARINE", note="等质量替换，注意风味变化"),
    SubstitutionIn(source_code="MARGARINE", target_code="BUTTER", note="回替示例（构成循环）"),
    SubstitutionIn(source_code="BUTTER", target_code="GHEE", note="澄清黄油，含乳"),
    SubstitutionIn(source_code="WHEAT_FLOUR", target_code="RICE_FLOUR", note="无麸质 1:1"),
    SubstitutionIn(source_code="WHEAT_FLOUR", target_code="ALMOND_FLOUR", note="引入坚果风险"),
    SubstitutionIn(source_code="EGG", target_code="APPLESAUCE", note="素食替代"),
    SubstitutionIn(source_code="SOY_SAUCE", target_code="TAMARI", note="减麸质方案"),
    SubstitutionIn(source_code="MILK", target_code="OAT_MILK", note="植物奶替代"),
]

RECIPES = [
    RecipeIn(
        code="COOKIE-001",
        version="1.0",
        name="经典黄油曲奇",
        note="传统配方，含主要过敏原",
        items=[
            RecipeItemIn(ingredient_code="WHEAT_FLOUR", quantity=200, unit="g"),
            RecipeItemIn(ingredient_code="BUTTER", quantity=120, unit="g"),
            RecipeItemIn(ingredient_code="EGG", quantity=1, unit="个"),
            RecipeItemIn(ingredient_code="SOY_SAUCE", quantity=5, unit="ml"),
        ],
    ),
    RecipeIn(
        code="COOKIE-001",
        version="2.0",
        name="低敏黄油曲奇",
        note="尝试无麸质与植物基替代，用于过敏原评估",
        items=[
            RecipeItemIn(ingredient_code="RICE_FLOUR", quantity=200, unit="g"),
            RecipeItemIn(ingredient_code="MARGARINE", quantity=120, unit="g"),
            RecipeItemIn(ingredient_code="APPLESAUCE", quantity=60, unit="g"),
            RecipeItemIn(ingredient_code="TAMARI", quantity=5, unit="ml"),
            RecipeItemIn(ingredient_code="MILK", quantity=30, unit="ml"),
        ],
    ),
]


def seed(conn) -> dict:
    counts = {"ingredients": 0, "recipes": 0, "substitutions": 0}
    for ing in INGREDIENTS:
        crud.upsert_ingredient(conn, ing)
        counts["ingredients"] += 1
    for sub in SUBSTITUTIONS:
        try:
            crud.create_substitution(conn, sub)
            counts["substitutions"] += 1
        except Exception:
            pass  # 已存在的重复边忽略，保证幂等
    for rec in RECIPES:
        crud.upsert_recipe(conn, rec)
        counts["recipes"] += 1
    conn.commit()
    return counts
