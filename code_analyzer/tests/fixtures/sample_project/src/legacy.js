const util = require("./app/util");
const path = require("path");

function run(n) {
  const v = util.util(n);
  return v;
}

module.exports = { run };
