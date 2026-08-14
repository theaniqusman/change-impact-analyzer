from sample_models import Calculator, ScientificCalculator
from sample_utils import add as plus  # aliased import - trickier resolution

calc = Calculator()
result = calc.compute(3, 4)
twice_result = calc.compute_twice(3, 4)
print(result, twice_result)

sci_calc = ScientificCalculator()
diff_result = sci_calc.compute_difference(10, 4)
inherited_result = sci_calc.compute(2, 2)  # inherited from Calculator, not defined here
print(diff_result, inherited_result)

extra = plus(5, 6)  # a SECOND, separate call site to add() -> fan-in
print(extra)
