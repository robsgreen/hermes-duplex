// Audio worklet for the Hermes Duplex page: microphone capture and playback in one node.
//
// Input: the microphone, at the AudioContext's rate. Each 20 ms of audio is resampled to 24 kHz
// and posted to the page as an ArrayBuffer of PCM16 mono (960 bytes).
// Output: PCM16 24 kHz mono ArrayBuffers the page posts in are resampled to the context's rate
// and played in order. Posting {type: "flush"} discards everything queued. The queue holds at most
// MAX_QUEUE_S of audio and drops the oldest beyond that.

const WIRE_RATE = 24000;
const FRAME_SAMPLES = WIRE_RATE / 50; // 20 ms at 24 kHz
const MAX_QUEUE_S = 5;

// Streaming linear-interpolation resampler; keeps its position across blocks so block edges
// add no clicks or gaps.
class Resampler {
  constructor(fromRate, toRate) {
    this.step = fromRate / toRate;
    this.reset();
  }

  reset() {
    // Position of the next output sample, in input samples from the start of the next block;
    // -1 is the last sample of the previous block (kept in `previous`).
    this.position = 0;
    this.previous = 0;
  }

  // Resample one block of Float32 samples and call `emit(sample)` for each output sample.
  push(input, emit) {
    let position = this.position;
    while (position < input.length - 1) {
      const index = Math.floor(position);
      const fraction = position - index;
      const a = index < 0 ? this.previous : input[index];
      emit(a + (input[index + 1] - a) * fraction);
      position += this.step;
    }
    if (input.length > 0) {
      this.position = position - input.length;
      this.previous = input[input.length - 1];
    }
  }
}

class DuplexProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capture = new Resampler(sampleRate, WIRE_RATE);
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.frameLength = 0;

    this.playback = new Resampler(WIRE_RATE, sampleRate);
    this.queue = []; // Float32Arrays at the context's rate
    this.queueOffset = 0; // samples of queue[0] already played
    this.queuedSamples = 0;
    this.maxQueuedSamples = MAX_QUEUE_S * sampleRate;

    this.port.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        this.enqueue(new Int16Array(event.data));
      } else if (event.data && event.data.type === "flush") {
        this.queue = [];
        this.queueOffset = 0;
        this.queuedSamples = 0;
        this.playback.reset();
      }
    };
  }

  enqueue(pcm16) {
    const input = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) input[i] = pcm16[i] / 32768;
    const output = [];
    this.playback.push(input, (sample) => output.push(sample));
    if (output.length === 0) return;
    this.queue.push(Float32Array.from(output));
    this.queuedSamples += output.length;
    while (this.queuedSamples > this.maxQueuedSamples && this.queue.length > 1) {
      this.queuedSamples -= this.queue[0].length - this.queueOffset;
      this.queue.shift();
      this.queueOffset = 0;
    }
  }

  captureBlock(samples) {
    this.capture.push(samples, (sample) => {
      const clipped = Math.max(-1, Math.min(1, sample));
      this.frame[this.frameLength++] = clipped < 0 ? clipped * 32768 : clipped * 32767;
      if (this.frameLength === FRAME_SAMPLES) {
        const buffer = this.frame.buffer.slice(0);
        this.port.postMessage(buffer, [buffer]);
        this.frameLength = 0;
      }
    });
  }

  playBlock(output) {
    let written = 0;
    while (written < output.length && this.queue.length > 0) {
      const chunk = this.queue[0];
      const count = Math.min(output.length - written, chunk.length - this.queueOffset);
      output.set(chunk.subarray(this.queueOffset, this.queueOffset + count), written);
      written += count;
      this.queueOffset += count;
      this.queuedSamples -= count;
      if (this.queueOffset === chunk.length) {
        this.queue.shift();
        this.queueOffset = 0;
      }
    }
    output.fill(0, written);
  }

  process(inputs, outputs) {
    const microphone = inputs[0];
    if (microphone && microphone.length > 0) this.captureBlock(microphone[0]);

    const speaker = outputs[0];
    if (speaker && speaker.length > 0) {
      this.playBlock(speaker[0]);
      for (let channel = 1; channel < speaker.length; channel++) speaker[channel].set(speaker[0]);
    }
    return true;
  }
}

registerProcessor("duplex-processor", DuplexProcessor);
