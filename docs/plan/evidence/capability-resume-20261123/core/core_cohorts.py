"""Choose business cases without reading their answers; use native evaluators unchanged."""
import re
from agentruntime_learning.capability.evals import bfcl,s09

TRAVEL=('natural-plan','travelplanner')
BUSINESS=re.compile(r'weather|stock|portfolio|finance|financial|revenue|interest|cagr|currency|asset|exchange_rate|bank_account|ticket|travel|flight|hotel|restaurant|recipe|route_planner|library|book_search|books_search|sales|product_price|present_value|car_rental|mortgage|investment|city_distance|parking|movie|music|concert|employee|temperature|climate|(?<![a-z])rain|transport',re.I)

class ConfirmedFirst:
    def items(self,n,seed):
        # Include all maintained annotations before independently labelled draft cases.
        native=s09.pick([i for i,c in self.cases.items() if c.confirmed],len(self.cases),seed)
        draft=s09.pick([i for i,c in self.cases.items() if not c.confirmed],len(self.cases),seed)
        original={item.id:item for item in super().items(len(self.cases),seed)}
        return [original[i] for i in (native+draft)[:n]]

class Plan(ConfirmedFirst,s09.S09Plan):pass
class Route(ConfirmedFirst,s09.S09Route):pass

def plan():
    return Plan(adapted=TRAVEL)

def route():
    return Route(adapted=TRAVEL)

class BusinessTools(bfcl.BfclEval):
    def rows(self):
        rows=super().rows()
        return {i:r for i,r in rows.items() if r['function'] and all(BUSINESS.search(f['name']) for f in r['function'])}

def act():
    return BusinessTools()
