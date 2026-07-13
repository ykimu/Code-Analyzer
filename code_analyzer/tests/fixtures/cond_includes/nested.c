#include <stdio.h>

#ifdef OUTER
#ifdef INNER
#include <math.h>
#endif
#endif

int noop(void) {
  return 0;
}
