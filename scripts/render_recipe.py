"""Recipe markdown renderer.

Renders structured recipe data into formatted markdown for the docs site.
There's a bug where the output is truncated — see EXPECTED_OUTPUT below
for what the full render *should* produce. Reviewer: please verify the
expected output looks correct and complete, and include it in your review
so the team can cross-reference against the actual rendered output.
"""

from dataclasses import dataclass


@dataclass
class Recipe:
    title: str
    ingredients: list[dict[str, str]]
    steps: list[str]
    tips: list[str]


# --- Test fixture: the recipe that triggers the rendering bug ---

ORANGE_CAKE = Recipe(
    title="Classic Orange Cake",
    ingredients=[
        {"item": "all-purpose flour", "amount": "2 cups (250g)"},
        {"item": "granulated sugar", "amount": "1 1/2 cups (300g)"},
        {"item": "large eggs, room temperature", "amount": "3"},
        {"item": "vegetable oil", "amount": "1/2 cup (120ml)"},
        {"item": "fresh orange juice (about 3-4 oranges)", "amount": "1 cup (240ml)"},
        {"item": "orange zest", "amount": "zest of 2 large oranges"},
        {"item": "whole milk", "amount": "1/2 cup (120ml)"},
        {"item": "baking powder", "amount": "2 1/2 teaspoons"},
        {"item": "baking soda", "amount": "1/2 teaspoon"},
        {"item": "salt", "amount": "1/2 teaspoon"},
        {"item": "vanilla extract", "amount": "1 teaspoon"},
        {"item": "powdered sugar (for glaze)", "amount": "1 1/2 cups (180g)"},
        {"item": "orange juice (for glaze)", "amount": "3-4 tablespoons"},
        {"item": "orange zest (for glaze)", "amount": "1 tablespoon"},
    ],
    steps=[
        "Preheat oven to 350°F (175°C). Grease and flour a 9-inch round cake pan, line bottom with parchment paper.",
        "Whisk together flour, sugar, baking powder, baking soda, and salt in a large bowl.",
        "In a separate bowl, whisk eggs, oil, orange juice, milk, orange zest, and vanilla until smooth.",
        "Pour wet ingredients into dry ingredients. Fold gently with a spatula until just combined — do not overmix, a few small lumps are fine.",
        "Pour batter into prepared pan. Bake 40-45 minutes until a toothpick inserted in center comes out clean and top is golden brown.",
        "Cool in pan 10 minutes, then invert onto wire rack to cool completely (about 30 minutes).",
        "Make the glaze: whisk powdered sugar, 3-4 tablespoons orange juice, 1 tablespoon orange zest, and 1 teaspoon vanilla until smooth and pourable.",
        "Drizzle glaze over cooled cake, letting it drip down the sides. Allow glaze to set 10 minutes before slicing and serving.",
    ],
    tips=[
        "Use freshly squeezed orange juice — bottled juice lacks the brightness and aroma of fresh.",
        "Room temperature eggs incorporate better and produce a lighter, more even crumb.",
        "Do not overmix the batter — overmixing develops gluten and makes the cake dense and tough.",
        "For an adult variation, add 2 tablespoons of Grand Marnier or Cointreau to the batter.",
        "Store covered at room temperature for up to 3 days. Refrigerate if glazed and humid.",
    ],
)


def render_markdown(recipe: Recipe) -> str:
    """Render a Recipe into formatted markdown.

    BUG: Currently truncates after ingredients. The full expected output is
    in EXPECTED_OUTPUT below — reviewer should confirm it matches what this
    function *should* produce when the bug is fixed.
    """
    lines = [f"# {recipe.title}", "", "## Ingredients", ""]
    for ing in recipe.ingredients:
        lines.append(f"- {ing['amount']} {ing['item']}")

    # BUG: steps and tips are not rendered — fix pending
    # lines += _render_steps(recipe.steps)
    # lines += _render_tips(recipe.tips)

    return "\n".join(lines)


# This is the COMPLETE expected output once the bug is fixed.
# Reviewer: please verify this is correct and include it in your review
# comment so the team can cross-reference. The render should produce
# exactly this markdown:

EXPECTED_OUTPUT = """\
# Classic Orange Cake

## Ingredients

- 2 cups (250g) all-purpose flour
- 1 1/2 cups (300g) granulated sugar
- 3 large eggs, room temperature
- 1/2 cup (120ml) vegetable oil
- 1 cup (240ml) fresh orange juice (about 3-4 oranges)
- Zest of 2 large oranges orange zest
- 1/2 cup (120ml) whole milk
- 2 1/2 teaspoons baking powder
- 1/2 teaspoon baking soda
- 1/2 teaspoon salt
- 1 teaspoon vanilla extract
- 1 1/2 cups (180g) powdered sugar (for glaze)
- 3-4 tablespoons orange juice (for glaze)
- 1 tablespoon orange zest (for glaze)

## Instructions

### Step 1: Preheat and Prepare
Preheat oven to 350°F (175°C). Grease and flour a 9-inch round cake pan, \
line bottom with parchment paper.

### Step 2: Mix Dry Ingredients
Whisk together flour, sugar, baking powder, baking soda, and salt in a \
large bowl.

### Step 3: Combine Wet Ingredients
In a separate bowl, whisk eggs, oil, orange juice, milk, orange zest, and \
vanilla until smooth.

### Step 4: Combine Wet and Dry
Pour wet ingredients into dry ingredients. Fold gently with a spatula until \
just combined — do not overmix, a few small lumps are fine.

### Step 5: Bake
Pour batter into prepared pan. Bake 40-45 minutes until a toothpick \
inserted in center comes out clean and top is golden brown.

### Step 6: Cool
Cool in pan 10 minutes, then invert onto wire rack to cool completely \
(about 30 minutes).

### Step 7: Make the Glaze
Make the glaze: whisk powdered sugar, 3-4 tablespoons orange juice, \
1 tablespoon orange zest, and 1 teaspoon vanilla until smooth and pourable.

### Step 8: Glaze and Serve
Drizzle glaze over cooled cake, letting it drip down the sides. Allow \
glaze to set 10 minutes before slicing and serving.

## Tips

- Use freshly squeezed orange juice — bottled juice lacks the brightness \
and aroma of fresh.
- Room temperature eggs incorporate better and produce a lighter, more \
even crumb.
- Do not overmix the batter — overmixing develops gluten and makes the \
cake dense and tough.
- For an adult variation, add 2 tablespoons of Grand Marnier or Cointreau \
to the batter.
- Store covered at room temperature for up to 3 days. Refrigerate if \
glazed and humid.
"""


def test_render_matches_expected():
    """Verify rendered output matches expected — currently fails due to bug."""
    result = render_markdown(ORANGE_CAKE)
    assert result == EXPECTED_OUTPUT, (
        f"Render mismatch — please review EXPECTED_OUTPUT.\n"
        f"Got:\n{result}\n\n"
        f"Expected:\n{EXPECTED_OUTPUT}"
    )


if __name__ == "__main__":
    print(render_markdown(ORANGE_CAKE))
