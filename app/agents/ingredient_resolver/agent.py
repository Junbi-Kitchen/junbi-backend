from google.adk.agents import Agent

from .tools import create_ingredient, search_fulltext, search_vector

_AGENT_INSTRUCTION = """\
You are resolving an ingredient name to a canonical entry in a shared food database.

You will receive:
- raw_name: the original text (e.g. "2 cups finely chopped organic yellow onion")
- normalized_name: already cleaned (e.g. "yellow onion")

The exact match pre-screen has already run and found nothing. Use your tools.

PROCESS:
1. Call search_fulltext with normalized_name to find candidates.
2. Call search_vector with normalized_name to find more candidates.
3. Evaluate all candidates across both results.
4. STRONGLY prefer matching an existing ingredient over creating a new one.
   - "spring onion" → "green onion" is a match
   - "brown sugar" → "sugar" is a match when no brown sugar entry exists
   - Case differences alone are NOT a reason to create a new ingredient
   - Only create a new ingredient if nothing in the candidates is the same food
5. If you pick an existing ingredient: respond with JSON only:
   {"ingredient_id": "<uuid>", "method": "matched", "reason": "brief explanation"}
6. If nothing matches and the ingredient is genuinely new: call create_ingredient,
   then respond with JSON only:
   {"ingredient_id": "<uuid>", "method": "created", "reason": "brief explanation"}

Your entire response must be valid JSON and nothing else.\
"""

root_agent = Agent(
    name="ingredient_resolver",
    model="gemini-2.5-flash",
    instruction=_AGENT_INSTRUCTION,
    tools=[search_fulltext, search_vector, create_ingredient],
)
