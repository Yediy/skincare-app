import { computeResizeDimensions } from "@/analysis/capture-resize";

describe("computeResizeDimensions", () => {
  it("scales a large landscape image down to a 1600px long edge", () => {
    expect(computeResizeDimensions({ width: 4000, height: 3000 }, 1600)).toEqual({ width: 1600, height: 1200 });
  });

  it("scales a large portrait image down to a 1600px long edge (caps height, not width)", () => {
    expect(computeResizeDimensions({ width: 3000, height: 4000 }, 1600)).toEqual({ width: 1200, height: 1600 });
  });

  it("caps a large square image consistently on either dimension", () => {
    expect(computeResizeDimensions({ width: 2000, height: 2000 }, 1600)).toEqual({ width: 1600, height: 1600 });
  });

  it("never upscales a small landscape source -- returns null (no resize action)", () => {
    expect(computeResizeDimensions({ width: 640, height: 480 }, 1600)).toBeNull();
  });

  it("never upscales a small portrait source -- returns null (no resize action)", () => {
    expect(computeResizeDimensions({ width: 480, height: 640 }, 1600)).toBeNull();
  });

  it("treats a source exactly at the bound as already within bounds", () => {
    expect(computeResizeDimensions({ width: 1600, height: 1200 }, 1600)).toBeNull();
  });
});
