#include "util.h"
#ifdef FEATURE_X
#include "feature.h"
#endif
#include <stdio.h>

int main(void) {
  util_fn();
  printf("hi\n");
  return 0;
}
