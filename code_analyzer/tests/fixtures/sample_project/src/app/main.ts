import { util } from "@app/util";
import { idx } from ".";
import * as ext from "lodash";

export class Widget {
  render(size: number): number {
    const scaled = util(size);
    return scaled;
  }
}

export function boot(): string {
  const w = new Widget();
  const label = idx();
  return label + ext.noop();
}
