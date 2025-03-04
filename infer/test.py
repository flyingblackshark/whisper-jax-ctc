import argparse
import time
import jax
import jax.numpy as jnp
from flax.core.frozen_dict import freeze
from jax.experimental.compilation_cache import compilation_cache as cc
from jax.sharding import PartitionSpec as P
from transformers import WhisperConfig, WhisperProcessor
import numpy as np
from whisper_jax import FlaxWhisperForConditionalGeneration, InferenceState, PjitPartitioner
import os
import librosa
#from silero_vad import load_silero_vad, read_audio, get_speech_timestamps
from jax.experimental import mesh_utils
import csv
from jax.sharding import Mesh, NamedSharding, PartitionSpec
cc.set_cache_dir("./jax_cache")
#jax.config.update("jax_array", True)
from vad import (
    SpeechTimestampsMap,
    VadOptions,
    collect_chunks,
    get_speech_timestamps,
    merge_segments,
)
import re
from align import load_align_model,align,SingleSegment
LANGUAGES = {
    "en": "english",
    "zh": "chinese",
    "de": "german",
    "es": "spanish",
    "ru": "russian",
    "ko": "korean",
    "fr": "french",
    "ja": "japanese",
    "pt": "portuguese",
    "tr": "turkish",
    "pl": "polish",
    "ca": "catalan",
    "nl": "dutch",
    "ar": "arabic",
    "sv": "swedish",
    "it": "italian",
    "id": "indonesian",
    "hi": "hindi",
    "fi": "finnish",
    "vi": "vietnamese",
    "he": "hebrew",
    "uk": "ukrainian",
    "el": "greek",
    "ms": "malay",
    "cs": "czech",
    "ro": "romanian",
    "da": "danish",
    "hu": "hungarian",
    "ta": "tamil",
    "no": "norwegian",
    "th": "thai",
    "ur": "urdu",
    "hr": "croatian",
    "bg": "bulgarian",
    "lt": "lithuanian",
    "la": "latin",
    "mi": "maori",
    "ml": "malayalam",
    "cy": "welsh",
    "sk": "slovak",
    "te": "telugu",
    "fa": "persian",
    "lv": "latvian",
    "bn": "bengali",
    "sr": "serbian",
    "az": "azerbaijani",
    "sl": "slovenian",
    "kn": "kannada",
    "et": "estonian",
    "mk": "macedonian",
    "br": "breton",
    "eu": "basque",
    "is": "icelandic",
    "hy": "armenian",
    "ne": "nepali",
    "mn": "mongolian",
    "bs": "bosnian",
    "kk": "kazakh",
    "sq": "albanian",
    "sw": "swahili",
    "gl": "galician",
    "mr": "marathi",
    "pa": "punjabi",
    "si": "sinhala",
    "km": "khmer",
    "sn": "shona",
    "yo": "yoruba",
    "so": "somali",
    "af": "afrikaans",
    "oc": "occitan",
    "ka": "georgian",
    "be": "belarusian",
    "tg": "tajik",
    "sd": "sindhi",
    "gu": "gujarati",
    "am": "amharic",
    "yi": "yiddish",
    "lo": "lao",
    "uz": "uzbek",
    "fo": "faroese",
    "ht": "haitian creole",
    "ps": "pashto",
    "tk": "turkmen",
    "nn": "nynorsk",
    "mt": "maltese",
    "sa": "sanskrit",
    "lb": "luxembourgish",
    "my": "myanmar",
    "bo": "tibetan",
    "tl": "tagalog",
    "mg": "malagasy",
    "as": "assamese",
    "tt": "tatar",
    "haw": "hawaiian",
    "ln": "lingala",
    "ha": "hausa",
    "ba": "bashkir",
    "jw": "javanese",
    "su": "sundanese",
    "yue": "cantonese",
}



logical_axis_rules_dp = [
    ("batch", "data"),
    ("mlp", None),
    ("heads", None),
    ("vocab", None),
    ("embed", None),
    ("embed", None),
    ("joined_kv", None),
    ("kv", None),
    ("length", None),
    ("num_mel", None),
    ("channels", None),
]
global_align_model_cache = {}

def get_align_model_with_cache(language_code):
    """获取对齐模型，如果已经加载过则直接从缓存中返回"""
    if language_code in global_align_model_cache:
        print(f"Using cached align model for language: {language_code}")
        return global_align_model_cache[language_code]
    
    print(f"Loading align model for language: {language_code}")
    model_a, metadata = load_align_model(language_code=language_code)
    global_align_model_cache[language_code] = (model_a, metadata)
    return model_a, metadata

def remove_symbols(text):
    # 使用正则表达式匹配 <|符号|> 并提取中间的内容
    cleaned_text = re.sub(r"<\|([^|]+)\|>", r"\1", text)
    return cleaned_text

def main():
    jax.distributed.initialize()
    BATCH_SIZE = 16
    LANGUAGE_DETECT_BATCH_SIZE = 8
    device_mesh = mesh_utils.create_device_mesh((jax.device_count(), 1))
    mesh = Mesh(device_mesh, axis_names=("data", "model")) 
    # processors/tokenizers are the same for all models, so just load from tiny and preprocess once
    processor = WhisperProcessor.from_pretrained("openai/whisper-large-v3")
    model, params = FlaxWhisperForConditionalGeneration.from_pretrained(
        "openai/whisper-large-v3",
        _do_init=False,
        dtype=jnp.bfloat16,
    )
    def all_language_tokens():
        result = []
        for token, token_id in zip(processor.tokenizer.all_special_tokens,processor.tokenizer.all_special_ids):
            if token.strip("<|>") in LANGUAGES:
                result.append(token_id)
        return tuple(result)
    def init_fn():
        input_shape = (1, 128, 3000)

        input_features = jnp.zeros(input_shape, dtype="f4")
        input_features = input_features.at[(..., -1)].set(model.config.eos_token_id)

        decoder_input_ids = jnp.zeros((input_shape[0], 1), dtype="i4")
        decoder_attention_mask = jnp.ones_like(decoder_input_ids)

        batch_size, sequence_length = decoder_input_ids.shape
        decoder_position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

        rng = jax.random.PRNGKey(0)
        init_params = model.module.init(
            rng,
            input_features=input_features,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            decoder_position_ids=decoder_position_ids,
            return_dict=False,
        )
        return init_params

    # Axis names metadata
    param_axes = jax.eval_shape(init_fn)["params_axes"]

    # Create InferenceState, since the partitioner expects it
    state = InferenceState(
        step=jnp.array(0),
        params=freeze(model.params_shape_tree),
        params_axes=freeze(param_axes),
        flax_mutables=None,
        flax_mutables_axes=param_axes,
    )

    partitioner = PjitPartitioner(
        num_partitions=1,
        #model_parallel_submesh=(2,2,1,1),
        logical_axis_rules=logical_axis_rules_dp,
    )

    mesh_axes = partitioner.get_mesh_axes(state)
    params_spec = mesh_axes.params

    p_shard_params = partitioner.partition(model.to_bf16, (params_spec,), params_spec)

    def generate(params, input_features,language):
        output_ids = model.generate(input_features, params=params,language=language).sequences
        return output_ids

    p_generate = partitioner.partition(
        generate,
        in_axis_resources=(params_spec, P("data")),
        out_axis_resources=P("data"),
        static_argnums=(2,)
    )
    params = jax.device_put(params,jax.devices()[0])
    # This will auto-magically run in mesh context
    params = p_shard_params(freeze(params))

    supported_formats = ('.wav', '.mp3', '.flac', '.ogg', '.m4a')
    output_csv = "transcriptions.csv"
    with open(output_csv, mode='w', newline='', encoding='utf-8') as csvfile:
        csv_writer = csv.writer(csvfile)
        csv_writer.writerow(["File Name", "Start Time", "End Time", "Language", "Transcription"])
        # 遍历文件夹
        for root, _, files in os.walk("/bucket/aurora_filtered"):
            for file in files:
                count_start_time = time.time()
                # 检查文件扩展名是否是支持的格式
                if file.lower().endswith(supported_formats):
                    file_path = os.path.join(root, file)

                    # 使用 librosa 加载音频文件
                    audio_data, sample_rate = librosa.load(file_path, sr=16000)  # sr=None 保持原始采样率
                    print(f"Successfully loaded {file_path}")
                    #print(f"Sample Rate: {sample_rate}, Audio Data Length: {len(audio_data)}")
                    vad_parameters = VadOptions(
                        max_speech_duration_s=30,
                        min_silence_duration_ms=160,
                    )
                    active_segments = get_speech_timestamps(audio_data, vad_parameters)
                    clip_timestamps = merge_segments(active_segments, vad_parameters)
                    
                    audio_segments = []
                    segments_info = []
                    logits = None
                    i = 0
                    
                    for timestamp in clip_timestamps:
                        #start, end = int(timestamp["start"] * sample_rate), int(timestamp["end"] * sample_rate)
                        segment = audio_data[timestamp["start"]:timestamp["end"]]
                        
                        # 对片段进行预处理
                        processed_segment = processor(segment, sampling_rate=16000, return_tensors="np")
                        audio_segments.append(processed_segment.input_features[0])
                        segments_info.append((file, timestamp["start"], timestamp["end"]))
                    count_segments = len(clip_timestamps)
                    count_vad_time = time.time()
                    def language_detect_wrap(params,input_features):
                        encoder_outputs = model.encode(input_features=input_features,params=params)
                        decoder_start_token_id = model.config.decoder_start_token_id
                        decoder_input_ids = jnp.ones((input_features.shape[0], 1), dtype="i4") * decoder_start_token_id
                        outputs = model.decode(decoder_input_ids, encoder_outputs,params=params)
                        return outputs.logits
                    x_sharding = NamedSharding(mesh,PartitionSpec("data"))
                    jitted_language_detect_func = jax.jit(language_detect_wrap,in_shardings=(None,x_sharding),out_shardings=x_sharding)
                    language_detect_segments = jnp.stack(audio_segments[:LANGUAGE_DETECT_BATCH_SIZE],axis=0)
                    LD_B_padding = LANGUAGE_DETECT_BATCH_SIZE - language_detect_segments.shape[0]
                    padded_language_detect_segments = jnp.pad(language_detect_segments,((0,LD_B_padding),(0,0),(0,0)))
                    if logits is None:
                        logits = jitted_language_detect_func(params,padded_language_detect_segments)
                    else:
                        logits += jitted_language_detect_func(params,padded_language_detect_segments)
                    def language_mask_wrap(logits):
                        logits = jnp.sum(logits,axis=0,keepdims=True)
                        mask = jnp.ones(logits.shape[-1], dtype=jnp.bool)
                        mask = mask.at[jnp.array(all_language_tokens())].set(False)
                        logits = jnp.where(mask,-jnp.inf,logits)
                        language_tokens = jnp.argmax(logits,axis=-1)
                        return language_tokens
                    language_tokens = jax.jit(language_mask_wrap)(logits)
                    detected_language = processor.decode(language_tokens[0,0])
                    count_detect_language_time = time.time()
                    
                    rounds = (len(audio_segments)-1) // BATCH_SIZE + 1
                    pred_ids_result = None
                    for i in range(rounds):
                        stacked_audio = audio_segments[i*BATCH_SIZE:(i+1)*BATCH_SIZE]
                        stacked_audio = np.stack(stacked_audio)
                        padding_size = BATCH_SIZE - stacked_audio.shape[0]
                        padded_stacked_audio = np.pad(stacked_audio,((0,padding_size),(0,0),(0,0)))
                        padded_stacked_audio = jnp.asarray(padded_stacked_audio)
                        pred_ids = p_generate(params, padded_stacked_audio,detected_language)
                        pred_ids = pred_ids[:BATCH_SIZE - padding_size]
                        pred_ids = np.asarray(pred_ids)
                        if pred_ids_result is None:
                            pred_ids_result = pred_ids
                        else:
                            pred_ids_result = np.concatenate([pred_ids_result,pred_ids],axis=0)
                    transcriptions = processor.batch_decode(pred_ids_result, skip_special_tokens=True)

                    count_transcribe_time = time.time()
                    model_a, metadata = get_align_model_with_cache(language_code=remove_symbols(detected_language))
                    segs = []
                    for (_ ,start_time, end_time), transcription in zip(segments_info, transcriptions):
                        segs.append(SingleSegment(start=start_time,end=end_time,text=transcription))
                    result = align(segs, model_a, metadata, audio_data, mesh, return_char_alignments=False)
                    count_align_time = time.time()
                    for segment in result["segments"]:
                        start_time = segment["start"] / 16000
                        end_time = segment["end"] / 16000
                        transcript = segment["text"]
                        csv_writer.writerow([file, start_time, end_time, transcript, detected_language])
                    runtime = time.time() 
                    print(f"VAD切割耗时:{(count_vad_time- count_start_time):.06} 检测语言耗时:{(count_detect_language_time - count_vad_time):.06} 转录耗时:{(count_transcribe_time - count_detect_language_time):.06} 对齐耗时:{(count_align_time - count_transcribe_time):.06} 总耗时: {(runtime- count_start_time):.06} 音频长度:{(len(audio_data)/16000):.06} sec VAD切割片段个数:{count_segments}")
    print(f"{runtime:.06}")


if __name__ == "__main__":
    main()
