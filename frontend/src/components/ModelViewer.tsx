import { Html, OrbitControls } from "@react-three/drei";
import { Canvas, useLoader, useThree } from "@react-three/fiber";
import type { ThreeEvent } from "@react-three/fiber";
import { Component, Suspense, useEffect, useMemo } from "react";
import type { ReactNode } from "react";
import * as THREE from "three";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";

import type { Characteristic } from "../lib/inspection";
import { Balloon } from "./common/Balloon";

type Point = [number, number, number];

/** Isometric-like view on the whole part, Z up (three.js scene objects are mutable). */
function fitCamera(
  geometry: THREE.BufferGeometry,
  camera: THREE.PerspectiveCamera,
  controls: { target: THREE.Vector3; update: () => void } | null,
): void {
  geometry.computeBoundingBox();
  const box = geometry.boundingBox!;
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3()).length();
  // Whole part in view: distance from the field of view, more for narrow viewports.
  const fit = (size / 2 / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) * 1.08;
  const distance = fit / Math.min(1, camera.aspect);
  camera.up.set(0, 0, 1);
  camera.position.copy(center).add(new THREE.Vector3(1, -1.25, 0.9).normalize().multiplyScalar(distance));
  camera.near = size / 100;
  camera.far = size * 20;
  camera.updateProjectionMatrix();
  if (controls) {
    controls.target.copy(center);
    controls.update();
  }
}

/** Part frame, Z up, millimetres: a hit point is directly a ModelRef.point (no offsets). */
function Part({ url, onPick }: { url: string; onPick?: (point: Point) => void }) {
  const geometry = useLoader(STLLoader, url);
  const edges = useMemo(() => new THREE.EdgesGeometry(geometry, 24), [geometry]);
  const { camera, controls, invalidate } = useThree() as unknown as {
    camera: THREE.PerspectiveCamera;
    controls: { target: THREE.Vector3; update: () => void } | null;
    invalidate: () => void;
  };
  useEffect(() => {
    fitCamera(geometry, camera, controls);
    invalidate(); // frameloop="demand": draw the fitted view
  }, [geometry, camera, controls, invalidate]);
  const click = onPick
    ? (e: ThreeEvent<MouseEvent>) => {
        e.stopPropagation();
        onPick([e.point.x, e.point.y, e.point.z]);
      }
    : undefined;
  return (
    <group>
      <mesh geometry={geometry} onClick={click}>
        <meshStandardMaterial color="#c4ced3" metalness={0.25} roughness={0.55} />
      </mesh>
      <lineSegments geometry={edges}>
        <lineBasicMaterial color="#1e2427" />
      </lineSegments>
    </group>
  );
}

class LoadError extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

export function ModelViewer({
  url,
  markers = [],
  selected,
  onSelect,
  onPick,
  empty,
}: {
  url: string;
  markers?: Characteristic[];
  selected?: string;
  onSelect?: (key: string) => void;
  onPick?: (point: Point) => void;
  empty: ReactNode;
}) {
  return (
    <LoadError key={url} fallback={<div className="viewport-empty">{empty}</div>}>
      <Canvas frameloop="demand" camera={{ fov: 32 }} style={{ cursor: onPick ? "crosshair" : "grab" }}>
        <color attach="background" args={["#fbfcfb"]} />
        <hemisphereLight args={["#ffffff", "#8a959b", 1.1]} />
        <directionalLight position={[300, -400, 600]} intensity={1.4} />
        <Suspense fallback={null}>
          <Part url={url} onPick={onPick} />
          {markers
            .filter((m) => m.modelPoint)
            .map((m) => (
              <Html key={m.key} position={m.modelPoint!} center zIndexRange={[10, 0]}>
                <Balloon
                  n={m.balloon}
                  status={m.status}
                  reviewer={m.kind === "reviewer"}
                  pressed={selected === m.key}
                  onClick={onSelect ? () => onSelect(m.key) : undefined}
                />
              </Html>
            ))}
        </Suspense>
        <OrbitControls makeDefault enableDamping={false} />
      </Canvas>
    </LoadError>
  );
}
