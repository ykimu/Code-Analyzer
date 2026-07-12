package com.ex;

import com.ex.Helper;
import java.util.List;

public class Main {
    public int run(int x) {
        Helper h = new Helper();
        return h.help(x);
    }

    public int run() {
        return run(0);
    }
}
