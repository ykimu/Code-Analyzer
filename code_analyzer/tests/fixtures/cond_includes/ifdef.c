#include <stdio.h>

#ifdef FEATURE_A
#include "guard.h"
#else
#include <stdlib.h>
#endif

int use_feature(void) {
  return 0;
}
