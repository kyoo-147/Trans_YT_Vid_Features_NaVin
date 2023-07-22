# -*- coding: utf-8 -*-
"""227-whisper-subtitles-generation.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/github/openvinotoolkit/openvino_notebooks/blob/main/notebooks/227-whisper-subtitles-generation/227-whisper-subtitles-generation.ipynb

# Video Subtitle Generation using Whisper and OpenVINO™

[Whisper](https://openai.com/blog/whisper/) is an automatic speech recognition (ASR) system trained on 680,000 hours of multilingual and multitask supervised data collected from the web. It is a multi-task model that can perform multilingual speech recognition as well as speech translation and language identification.

![asr-training-data-desktop.svg](https://user-images.githubusercontent.com/29454499/204536347-28976978-9a07-416c-acff-fc1214bbfbe0.svg)

You can find more information about this model in the [research paper](https://cdn.openai.com/papers/whisper.pdf), [OpenAI blog](https://openai.com/blog/whisper/), [model card](https://github.com/openai/whisper/blob/main/model-card.md) and GitHub [repository](https://github.com/openai/whisper).

In this notebook, we will use Whisper with OpenVINO to generate subtitles in a sample video.
Notebook contains the following steps:
1. Download the model.
2. Instantiate the PyTorch model pipeline.
3. Export the ONNX model and convert it to OpenVINO IR, using the Model Optimizer tool.
4. Run the Whisper pipeline with OpenVINO models.

## Prerequisites

Clone and install the model repository.
"""

!pip install -q "openvino-dev>=2023.0.0"
!pip install -q "python-ffmpeg<=1.0.16" moviepy transformers onnx
!pip install -q -I "git+https://github.com/garywu007/pytube.git"

from pathlib import Path

REPO_DIR = Path("whisper")
if not REPO_DIR.exists():
    !git clone https://github.com/openai/whisper.git -b v20230124
!cd whisper && pip install .

"""## Instantiate model
Whisper is a Transformer based encoder-decoder model, also referred to as a sequence-to-sequence model. It maps a sequence of audio spectrogram features to a sequence of text tokens. First, the raw audio inputs are converted to a log-Mel spectrogram by action of the feature extractor. Then, the Transformer encoder encodes the spectrogram to form a sequence of encoder hidden states. Finally, the decoder autoregressively predicts text tokens, conditional on both the previous tokens and the encoder hidden states.

You can see the model architecture in the diagram below:

![whisper_architecture.svg](https://user-images.githubusercontent.com/29454499/204536571-8f6d8d77-5fbd-4c6d-8e29-14e734837860.svg)

There are several models of different sizes and capabilities trained by the authors of the model. In this tutorial, we will use the `base` model, but the same actions are also applicable to other models from Whisper family.
"""

import whisper

model = whisper.load_model("base")
model.to("cpu")
model.eval()
pass

"""### Convert model to OpenVINO Intermediate Representation (IR) format.

For best results with OpenVINO, it is recommended to convert the model to OpenVINO IR format. OpenVINO supports PyTorch via ONNX conversion. We will use `torch.onnx.export` for exporting the ONNX model from PyTorch. We need to provide initialized model object and example of inputs for shape inference. We will use `mo.convert_model` functionality to convert the ONNX models. The `mo.convert_model` Python function returns an OpenVINO model ready to load on device and start making predictions. We can save it on disk for next usage with `openvino.runtime.serialize`.

### Convert Whisper Encoder to OpenVINO IR
"""

import torch
from openvino.tools import mo
from openvino.runtime import serialize

mel = torch.zeros((1, 80, 3000))
audio_features = model.encoder(mel)
torch.onnx.export(
    model.encoder,
    mel,
    "whisper_encoder.onnx",
    input_names=["mel"],
    output_names=["output_features"]
)
encoder_model = mo.convert_model("whisper_encoder.onnx", compress_to_fp16=True)
serialize(encoder_model, xml_path="whisper_encoder.xml")

"""### Convert Whisper decoder to OpenVINO IR

To reduce computational complexity, the decoder uses cached key/value projections in attention modules from the previous steps. We need to modify this process for correct tracing to ONNX.
"""

import torch
from typing import Optional, Union, List, Dict
from functools import partial

positional_embeddings_size = model.decoder.positional_embedding.shape[0]


def save_to_cache(cache: Dict[str, torch.Tensor], module: str, output: torch.Tensor):
    """
    Saving cached attention hidden states for previous tokens.
    Parameters:
      cache: dictionary with cache.
      module: current attention module name.
      output: predicted hidden state.
    Returns:
      output: cached attention hidden state for specified attention module.
    """
    if module not in cache or output.shape[1] > positional_embeddings_size:
        # save as-is, for the first token or cross attention
        cache[module] = output
    else:
        cache[module] = torch.cat([cache[module], output], dim=1).detach()
    return cache[module]


def attention_forward(
        attention_module,
        x: torch.Tensor,
        xa: Optional[torch.Tensor] = None,
        mask: Optional[torch.Tensor] = None,
        kv_cache: Optional[dict] = None,
        idx: int = 0
):
    """
    Override for forward method of decoder attention module with storing cache values explicitly.
    Parameters:
      attention_module: current attention module
      x: input token ids.
      xa: input audio features (Optional).
      mask: mask for applying attention (Optional).
      kv_cache: dictionary with cached key values for attention modules.
      idx: idx for search in kv_cache.
    Returns:
      attention module output tensor
      updated kv_cache
    """
    q = attention_module.query(x)

    if kv_cache is None or xa is None:
        # hooks, if installed (i.e. kv_cache is not None), will prepend the cached kv tensors;
        # otherwise, perform key/value projections for self- or cross-attention as usual.
        k = attention_module.key(x if xa is None else xa)
        v = attention_module.value(x if xa is None else xa)
        if kv_cache is not None:
            k = save_to_cache(kv_cache, f'k_{idx}', k)
            v = save_to_cache(kv_cache, f'v_{idx}', v)
    else:
        # for cross-attention, calculate keys and values once and reuse in subsequent calls.
        k = kv_cache.get(f'k_{idx}', save_to_cache(
            kv_cache, f'k_{idx}', attention_module.key(xa)))
        v = kv_cache.get(f'v_{idx}', save_to_cache(
            kv_cache, f'v_{idx}', attention_module.value(xa)))

    wv, qk = attention_module.qkv_attention(q, k, v, mask)
    return attention_module.out(wv), kv_cache


def block_forward(
    residual_block,
    x: torch.Tensor,
    xa: Optional[torch.Tensor] = None,
    mask: Optional[torch.Tensor] = None,
    kv_cache: Optional[dict] = None,
    idx: int = 0
):
    """
    Override for residual block forward method for providing kv_cache to attention module.
      Parameters:
        residual_block: current residual block.
        x: input token_ids.
        xa: input audio features (Optional).
        mask: attention mask (Optional).
        kv_cache: cache for storing attention key values.
        idx: index of current residual block for search in kv_cache.
      Returns:
        x: residual block output
        kv_cache: updated kv_cache

    """
    x0, kv_cache = residual_block.attn(residual_block.attn_ln(
        x), mask=mask, kv_cache=kv_cache, idx=f'{idx}a')
    x = x + x0
    if residual_block.cross_attn:
        x1, kv_cache = residual_block.cross_attn(
            residual_block.cross_attn_ln(x), xa, kv_cache=kv_cache, idx=f'{idx}c')
        x = x + x1
    x = x + residual_block.mlp(residual_block.mlp_ln(x))
    return x, kv_cache


# update forward functions
for idx, block in enumerate(model.decoder.blocks):
    block.forward = partial(block_forward, block, idx=idx)
    block.attn.forward = partial(attention_forward, block.attn)
    if block.cross_attn:
        block.cross_attn.forward = partial(attention_forward, block.cross_attn)


def decoder_forward(decoder, x: torch.Tensor, xa: torch.Tensor, kv_cache: Optional[dict] = None):
    """
    Override for decoder forward method.
    Parameters:
      x: torch.LongTensor, shape = (batch_size, <= n_ctx) the text tokens
      xa: torch.Tensor, shape = (batch_size, n_mels, n_audio_ctx)
           the encoded audio features to be attended on
      kv_cache: Dict[str, torch.Tensor], attention modules hidden states cache from previous steps
    """
    offset = next(iter(kv_cache.values())).shape[1] if kv_cache else 0
    x = decoder.token_embedding(
        x) + decoder.positional_embedding[offset: offset + x.shape[-1]]
    x = x.to(xa.dtype)

    for block in decoder.blocks:
        x, kv_cache = block(x, xa, mask=decoder.mask, kv_cache=kv_cache)

    x = decoder.ln(x)
    logits = (
        x @ torch.transpose(decoder.token_embedding.weight.to(x.dtype), 1, 0)).float()

    return logits, kv_cache


# override decoder forward
model.decoder.forward = partial(decoder_forward, model.decoder)

tokens = torch.ones((5, 3), dtype=torch.int64)

logits, kv_cache = model.decoder(tokens, audio_features, kv_cache={})
kv_cache = {k: v for k, v in kv_cache.items()}
tokens = torch.ones((5, 1), dtype=torch.int64)

outputs = [f"out_{k}" for k in kv_cache.keys()]
inputs = [f"in_{k}" for k in kv_cache.keys()]
dynamic_axes = {
    "tokens": {0: "beam_size", 1: "seq_len"},
    "audio_features": {0: "beam_size"},
    "logits": {0: "beam_size", 1: "seq_len"}}
dynamic_outs = {o: {0: "beam_size", 1: "prev_seq_len"} for o in outputs}
dynamic_inp = {i: {0: "beam_size", 1: "prev_seq_len"} for i in inputs}
dynamic_axes.update(dynamic_outs)
dynamic_axes.update(dynamic_inp)
torch.onnx.export(
    model.decoder, {'x': tokens, 'xa': audio_features, 'kv_cache': kv_cache},
    'whisper_decoder.onnx',
    input_names=["tokens", "audio_features"] + inputs,
    output_names=["logits"] + outputs,
    dynamic_axes=dynamic_axes
)

"""The decoder model autoregressively predicts the next token guided by encoder hidden states and previously predicted sequence. This means that the shape of inputs which depends on the previous step (inputs for tokens and attention hidden states from previous step) are dynamic. For efficient utilization of memory, you define an upper bound for dynamic input shapes."""

input_shapes = "tokens[1..5 1..224],audio_features[1..5 1500 512]"
for k, v in kv_cache.items():
    if k.endswith('a'):
        input_shapes += f",in_{k}[1..5 0..224 512]"
decoder_model = mo.convert_model(
    input_model="whisper_decoder.onnx",
    compress_to_fp16=True,
    input=input_shapes)
serialize(decoder_model, "whisper_decoder.xml")

"""## Prepare inference pipeline

The image below illustrates the pipeline of video transcribing using the Whisper model.

![whisper_pipeline.png](https://user-images.githubusercontent.com/29454499/204536733-1f4342f7-2328-476a-a431-cb596df69854.png)

To run the PyTorch Whisper model, we just need to call the `model.transcribe(audio, **parameters)` function. We will try to reuse original model pipeline for audio transcribing after replacing the original models with OpenVINO IR versions.
"""

class OpenVINOAudioEncoder(torch.nn.Module):
    """
    Helper for inference Whisper encoder model with OpenVINO
    """

    def __init__(self, core, model_path, device='CPU'):
        super().__init__()
        self.model = core.read_model(model_path)
        self.compiled_model = core.compile_model(self.model, device)
        self.output_blob = self.compiled_model.output(0)

    def forward(self, mel: torch.Tensor):
        """
        Inference OpenVINO whisper encoder model.

        Parameters:
          mel: input audio fragment mel spectrogram.
        Returns:
          audio_features: torch tensor with encoded audio features.
        """
        return torch.from_numpy(self.compiled_model(mel)[self.output_blob])

from openvino.runtime import Core, Tensor


class OpenVINOTextDecoder(torch.nn.Module):
    """
    Helper for inference OpenVINO decoder model
    """

    def __init__(self, core: Core, model_path: Path, device: str = 'CPU'):
        super().__init__()
        self._core = core
        self.model = core.read_model(model_path)
        self._input_names = [inp.any_name for inp in self.model.inputs]
        self.compiled_model = core.compile_model(self.model, device)
        self.device = device

    def init_past_inputs(self, feed_dict):
        """
        Initialize cache input for first step.

        Parameters:
          feed_dict: Dictonary with inputs for inference
        Returns:
          feed_dict: updated feed_dict
        """
        beam_size = feed_dict['tokens'].shape[0]
        audio_len = feed_dict['audio_features'].shape[2]
        previous_seq_len = 0
        for name in self._input_names:
            if name in ['tokens', 'audio_features']:
                continue
            feed_dict[name] = Tensor(np.zeros(
                (beam_size, previous_seq_len, audio_len), dtype=np.float32))
        return feed_dict

    def preprocess_kv_cache_inputs(self, feed_dict, kv_cache):
        """
        Transform kv_cache to inputs

        Parameters:
          feed_dict: dictionary with inputs for inference
          kv_cache: dictionary with cached attention hidden states from previous step
        Returns:
          feed_dict: updated feed dictionary with additional inputs
        """
        if not kv_cache:
            return self.init_past_inputs(feed_dict)
        for k, v in kv_cache.items():
            new_k = f'in_{k}'
            if new_k in self._input_names:
                feed_dict[new_k] = Tensor(v.numpy())
        return feed_dict

    def postprocess_outputs(self, outputs):
        """
        Transform model output to format expected by the pipeline

        Parameters:
          outputs: outputs: raw inference results.
        Returns:
          logits: decoder predicted token logits
          kv_cache: cached attention hidden states
        """
        logits = None
        kv_cache = {}
        for output_t, out in outputs.items():
            if 'logits' in output_t.get_names():
                logits = torch.from_numpy(out)
            else:
                tensor_name = output_t.any_name
                kv_cache[tensor_name.replace(
                    'out_', '')] = torch.from_numpy(out)
        return logits, kv_cache

    def forward(self, x: torch.Tensor, xa: torch.Tensor, kv_cache: Optional[dict] = None):
        """
        Inference decoder model.

        Parameters:
          x: torch.LongTensor, shape = (batch_size, <= n_ctx) the text tokens
          xa: torch.Tensor, shape = (batch_size, n_mels, n_audio_ctx)
             the encoded audio features to be attended on
          kv_cache: Dict[str, torch.Tensor], attention modules hidden states cache from previous steps
        Returns:
          logits: decoder predicted logits
          kv_cache: updated kv_cache with current step hidden states
        """
        feed_dict = {'tokens': Tensor(x.numpy()), 'audio_features': Tensor(xa.numpy())}
        feed_dict = (self.preprocess_kv_cache_inputs(feed_dict, kv_cache))
        res = self.compiled_model(feed_dict)
        return self.postprocess_outputs(res)

from whisper.decoding import DecodingTask, Inference, DecodingOptions, DecodingResult


class OpenVINOInference(Inference):
    """
    Wrapper for inference interface
    """

    def __init__(self, model: "Whisper", initial_token_length: int):
        self.model: "Whisper" = model
        self.initial_token_length = initial_token_length
        self.kv_cache = {}

    def logits(self, tokens: torch.Tensor, audio_features: torch.Tensor) -> torch.Tensor:
        """
        getting logits for given tokens sequence and audio features and save kv_cache

        Parameters:
          tokens: input tokens
          audio_features: input audio features
        Returns:
          logits: predicted by decoder logits
        """
        if tokens.shape[-1] > self.initial_token_length:
            # only need to use the last token except in the first forward pass
            tokens = tokens[:, -1:]
        logits, self.kv_cache = self.model.decoder(
            tokens, audio_features, kv_cache=self.kv_cache)
        return logits

    def cleanup_caching(self):
        """
        Reset kv_cache to initial state
        """
        self.kv_cache = {}

    def rearrange_kv_cache(self, source_indices):
        """
        Update hidden states cache for selected sequences
        Parameters:
          source_indicies: sequences indicies
        Returns:
          None
        """
        for module, tensor in self.kv_cache.items():
            # update the key/value cache to contain the selected sequences
            self.kv_cache[module] = tensor[source_indices]


class OpenVINODecodingTask(DecodingTask):
    """
    Class for decoding using OpenVINO
    """

    def __init__(self, model: "Whisper", options: DecodingOptions):
        super().__init__(model, options)
        self.inference = OpenVINOInference(model, len(self.initial_tokens))


@torch.no_grad()
def decode(model: "Whisper", mel: torch.Tensor, options: DecodingOptions = DecodingOptions()) -> Union[DecodingResult, List[DecodingResult]]:
    """
    Performs decoding of 30-second audio segment(s), provided as Mel spectrogram(s).

    Parameters
    ----------
    model: Whisper
        the Whisper model instance

    mel: torch.Tensor, shape = (80, 3000) or (*, 80, 3000)
        A tensor containing the Mel spectrogram(s)

    options: DecodingOptions
        A dataclass that contains all necessary options for decoding 30-second segments

    Returns
    -------
    result: Union[DecodingResult, List[DecodingResult]]
        The result(s) of decoding contained in `DecodingResult` dataclass instance(s)
    """
    single = mel.ndim == 2
    if single:
        mel = mel.unsqueeze(0)

    result = OpenVINODecodingTask(model, options).run(mel)

    if single:
        result = result[0]

    return result

del model.decoder
del model.encoder

core = Core()

"""### Select inference device

select device from dropdown list for running inference using OpenVINO
"""

import ipywidgets as widgets

device = widgets.Dropdown(
    options=core.available_devices + ["AUTO"],
    value='AUTO',
    description='Device:',
    disabled=False,
)

device

from collections import namedtuple

Parameter = namedtuple('Parameter', ['device'])

model.encoder = OpenVINOAudioEncoder(core, 'whisper_encoder.xml', device=device.value)
model.decoder = OpenVINOTextDecoder(core, 'whisper_decoder.xml', device=device.value)
model.decode = partial(decode, model)


def parameters():
    return iter([Parameter(torch.device('cpu'))])


model.parameters = parameters


def logits(model, tokens: torch.Tensor, audio_features: torch.Tensor):
    """
    Override for logits extraction method
    Parameters:
      toekns: input tokens
      audio_features: input audio features
    Returns:
      logits: decoder predicted logits
    """
    return model.decoder(tokens, audio_features, None)[0]


model.logits = partial(logits, model)

"""#### Define audio preprocessing

The model expects mono-channel audio with a 16000 Hz sample rate, represented in floating point range. When the audio from the input video does not meet these requirements, we will need to apply preprocessing.
"""

import io
from pathlib import Path
import numpy as np
from scipy.io import wavfile
from pytube import YouTube
from moviepy.editor import VideoFileClip


def resample(audio, src_sample_rate, dst_sample_rate):
    """
    Resample audio to specific sample rate

    Parameters:
      audio: input audio signal
      src_sample_rate: source audio sample rate
      dst_sample_rate: destination audio sample rate
    Returns:
      resampled_audio: input audio signal resampled with dst_sample_rate
    """
    if src_sample_rate == dst_sample_rate:
        return audio
    duration = audio.shape[0] / src_sample_rate
    resampled_data = np.zeros(shape=(int(duration * dst_sample_rate)), dtype=np.float32)
    x_old = np.linspace(0, duration, audio.shape[0], dtype=np.float32)
    x_new = np.linspace(0, duration, resampled_data.shape[0], dtype=np.float32)
    resampled_audio = np.interp(x_new, x_old, audio)
    return resampled_audio.astype(np.float32)


def audio_to_float(audio):
    """
    convert audio signal to floating point format
    """
    return audio.astype(np.float32) / np.iinfo(audio.dtype).max


def get_audio(video_file):
    """
    Extract audio signal from a given video file, then convert it to float,
    then mono-channel format and resample it to the expected sample rate

    Parameters:
        video_file: path to input video file
    Returns:
      resampled_audio: mono-channel float audio signal with 16000 Hz sample rate
                       extracted from video
    """
    input_video = VideoFileClip(str(video_file))
    input_video.audio.write_audiofile(video_file.stem + '.wav', verbose=False, logger=None)
    input_audio_file = video_file.stem + '.wav'
    sample_rate, audio = wavfile.read(
        io.BytesIO(open(input_audio_file, 'rb').read()))
    audio = audio_to_float(audio)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    resampled_audio = resample(audio, sample_rate, 16000)
    return resampled_audio

"""## Run video transcription pipeline

Now, we are ready to start transcription. We select a video from YouTube that we want to transcribe. Be patient, as downloading the video may take some time.
"""

import ipywidgets as widgets
VIDEO_LINK = "https://youtu.be/kgL5LBM-hFI"
link = widgets.Text(
    value=VIDEO_LINK,
    placeholder="Type link for video",
    description="Video:",
    disabled=False
)

link

print(f"Downloading video {link.value} started")

output_file = Path("downloaded_video.mp4")
yt = YouTube(link.value)
yt.streams.get_highest_resolution().download(filename=output_file)
print(f"Video saved to {output_file}")

audio = get_audio(output_file)

"""Select the task for the model:
* **transcribe** - generate audio transcription in the source language (automatically detected).
* **translate** - generate audio transcription with translation to English language.
"""

task = widgets.Select(
    options=["transcribe", "translate"],
    value="translate",
    description="Select task:",
    disabled=False
)
task

transcription = model.transcribe(audio, beam_size=5, best_of=5, task=task.value)

def format_timestamp(seconds: float):
    """
    format time in srt-file excpected format
    """
    assert seconds >= 0, "non-negative timestamp expected"
    milliseconds = round(seconds * 1000.0)

    hours = milliseconds // 3_600_000
    milliseconds -= hours * 3_600_000

    minutes = milliseconds // 60_000
    milliseconds -= minutes * 60_000

    seconds = milliseconds // 1_000
    milliseconds -= seconds * 1_000

    return (f"{hours}:" if hours > 0 else "00:") + f"{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def prepare_srt(transcription):
    """
    Format transcription into srt file format
    """
    segment_lines = []
    for segment in transcription["segments"]:
        segment_lines.append(str(segment["id"] + 1) + "\n")
        time_start = format_timestamp(segment["start"])
        time_end = format_timestamp(segment["end"])
        time_str = f"{time_start} --> {time_end}\n"
        segment_lines.append(time_str)
        segment_lines.append(segment["text"] + "\n\n")
    return segment_lines

""""The results will be saved in the `downloaded_video.srt` file. SRT is one of the most popular formats for storing subtitles and is compatible with many modern video players. This file can be used to embed transcription into videos during playback or by injecting them directly into video files using `ffmpeg`."""

srt_lines = prepare_srt(transcription)
# save transcription
with output_file.with_suffix(".srt").open("w") as f:
    f.writelines(srt_lines)

"""Now let us see the results."""

widgets.Video.from_file(output_file, loop=False, width=800, height=800)

print("".join(srt_lines))