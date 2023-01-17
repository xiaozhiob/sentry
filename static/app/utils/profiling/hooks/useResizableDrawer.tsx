import {useCallback, useLayoutEffect, useRef, useState} from 'react';

export interface UseResizableDrawerOptions {
  /**
   * When dragging, which direction should be used for the delta
   */
  direction: 'right' | 'left' | 'down' | 'up';
  /**
   * The minimum sizes the container may be dragged to
   */
  min: number;
  /**
   * Triggered while dragging
   */
  onResize: (newSize: number, maybeOldSize?: number) => void;
  /**
   * The starting size of the container. If not specified will be assumed as 0
   */
  initialSize?: number;
}

/**
 * Hook to support draggable container resizing
 *
 * This only resizes one dimension at a time.
 */
export function useResizableDrawer(options: UseResizableDrawerOptions): {
  /**
   * Apply to the drag handle element
   */
  onMouseDown: React.MouseEventHandler<HTMLElement>;
  /**
   * The resulting size of the container axis. Updated while dragging.
   *
   * NOTE: Be careful using this as this as react state updates are not
   * synchronous, you may want to update the element size using onResize instead
   */
  size: number;
} {
  const rafIdRef = useRef<number | null>(null);
  const currentMouseVectorRaf = useRef<[number, number] | null>(null);
  const [size, setSize] = useState<number>(options.initialSize ?? 0);

  // We intentionally fire this once at mount to ensure the dimensions are set and
  // any potentional values set by CSS will be overriden. If no initialDimensions are provided,
  // invoke the onResize callback with the previously stored dimensions.
  useLayoutEffect(() => {
    options.onResize(options.initialSize ?? 0, size);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.direction]);

  const sizeRef = useRef<number>(size);
  sizeRef.current = size;

  const onMouseMove = useCallback(
    (event: MouseEvent) => {
      const isXAxis = options.direction === 'left' || options.direction === 'right';
      const isInverted = options.direction === 'down' || options.direction === 'left';

      document.body.style.userSelect = 'none';

      if (rafIdRef.current !== null) {
        window.cancelAnimationFrame(rafIdRef.current);
      }

      rafIdRef.current = window.requestAnimationFrame(() => {
        if (!currentMouseVectorRaf.current) {
          return;
        }
        const newPositionVector: [number, number] = [event.clientX, event.clientY];

        const positionDelta = [
          currentMouseVectorRaf.current[0] - newPositionVector[0],
          currentMouseVectorRaf.current[1] - newPositionVector[1],
        ];

        currentMouseVectorRaf.current = newPositionVector;

        const sizeDelta = isXAxis ? positionDelta[0] : positionDelta[1];

        // Round to 1px precision
        const newSize = Math.round(
          Math.max(options.min, sizeRef.current + sizeDelta * (isInverted ? -1 : 1))
        );

        options.onResize(newSize);
        setSize(newSize);
      });
    },
    [options]
  );

  const onMouseUp = useCallback(() => {
    document.body.style.pointerEvents = '';
    document.body.style.userSelect = '';
    document.removeEventListener('mousemove', onMouseMove);
    document.removeEventListener('mouseup', onMouseUp);
  }, [onMouseMove]);

  const onMouseDown = useCallback(
    (evt: React.MouseEvent<HTMLElement>) => {
      currentMouseVectorRaf.current = [evt.clientX, evt.clientY];

      document.addEventListener('mousemove', onMouseMove, {passive: true});
      document.addEventListener('mouseup', onMouseUp);
    },
    [onMouseMove, onMouseUp]
  );

  useLayoutEffect(() => {
    return () => {
      if (rafIdRef.current !== null) {
        window.cancelAnimationFrame(rafIdRef.current);
      }
    };
  });

  return {size, onMouseDown};
}
