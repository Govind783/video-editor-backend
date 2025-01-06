import os
import shutil
import subprocess
from flask import Flask, request, send_file
import json
from werkzeug.utils import secure_filename
import uuid
from flask_cors import CORS


app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


# Configure upload folder
UPLOAD_FOLDER = 'temp_uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080

def scale_coordinates(x, y, input_width, input_height):
    """Scale coordinates from input dimensions to output dimensions"""
    scaled_x = (x / input_width) * OUTPUT_WIDTH
    scaled_y = (y / input_height) * OUTPUT_HEIGHT
    return int(scaled_x), int(scaled_y)

def scale_dimensions(width, height, input_width, input_height):
    """Scale dimensions from input to output resolution"""
    scaled_width = (width / input_width) * OUTPUT_WIDTH
    scaled_height = (height / input_height) * OUTPUT_HEIGHT
    return int(scaled_width), int(scaled_height)

def scale_value(value, input_dim, output_dim):
    """
    Scale a single value from input dimension to output dimension
    
    Args:
        value (float/int): The value to scale (e.g. width, height, x, y coordinate)
        input_dim (float): The input dimension (e.g. input width or height)
        output_dim (float): The target output dimension
        
    Returns:
        int: The scaled value rounded to nearest integer
    """
    if not input_dim:  # Prevent division by zero
        return int(value)
    
    scaled = (value / input_dim) * output_dim
    return int(scaled)


@app.route('/process', methods=['POST'])
def process_video():
    work_dir = None
    try:
        work_dir = os.path.abspath(os.path.join(UPLOAD_FOLDER, str(uuid.uuid4())))
        os.makedirs(work_dir)

        metadata = json.loads(request.form['metadata'])
        
        input_width = float(request.form.get('canvas_width', 1920))
        input_height = float(request.form.get('canvas_height', 1080))
        
        video_paths = []
        videos = request.files.getlist('videos')
        for idx, video in enumerate(videos):
            filename = f"video_{idx}.mp4"
            filepath = os.path.join(work_dir, filename)
            video.save(filepath)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                video_paths.append(filepath)
            else:
                raise Exception(f"Failed to save video {idx}")

        image_paths = []
        images = request.files.getlist('images')
        for idx, image in enumerate(images):
            filename = f"image_{idx}.png"
            filepath = os.path.join(work_dir, filename)
            image.save(filepath)
            if os.path.exists(filepath):
                image_paths.append(filepath)

        output_path = os.path.join(work_dir, 'output.mp4')
        filter_parts = []

        
        base_duration = metadata["videos"][0]["duration"] if metadata["videos"] else 10
        filter_parts.append(f'color=c=black:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:d={base_duration}[canvas];')

        
        for idx, (path, meta) in enumerate(zip(video_paths, metadata['videos'])):
            
            x, y = scale_coordinates(meta["x"], meta["y"], input_width, input_height)
            if "width" in meta and "height" in meta:
                width, height = scale_dimensions(meta["width"], meta["height"], input_width, input_height)
            else:
                width, height = -1, -1  
            
            speed = float(meta.get("speed", 1.0))
            pts_speed = 1/speed  
            volume = float(meta.get("volume", 100)) / 100 

            filter_parts.extend([
                f'[{idx}:v]setpts={pts_speed}*PTS,scale={width}:{height}[v{idx}];',
                f'[{idx}:a]asetpts={pts_speed}*PTS,atempo={speed},volume={volume}[a{idx}];'
            ])

            if idx == 0:
                filter_parts.append(
                    f'[canvas][v{idx}]overlay=x={x}:y={y}:eval=init[temp{idx}];'
                )
            else:
                filter_parts.append(
                    f'[temp{idx-1}][v{idx}]overlay=x={x}:y={y}:'
                    f'enable=\'between(t,{meta["startTime"]},{meta["endTime"]})\':eval=init'
                    f'[temp{idx}];'
                )

        last_video_temp = f'temp{len(video_paths)-1}'

        
        if video_paths:
            audio_mix_parts = []
            for idx in range(len(video_paths)):
                audio_mix_parts.append(f'[a{idx}]')
            filter_parts.append(f'{"".join(audio_mix_parts)}amix=inputs={len(video_paths)}[aout];')

        
        if image_paths:
            for idx, (img_path, img_meta) in enumerate(zip(image_paths, metadata['images'])):
                input_idx = len(video_paths) + idx
                
                
                x, y = scale_coordinates(img_meta["x"], img_meta["y"], input_width, input_height)
                width, height = scale_dimensions(img_meta["width"], img_meta["height"], input_width, input_height)
                
                border_radius = scale_value(img_meta.get("borderRadius", 0), input_width, OUTPUT_WIDTH)
                opacity = float(img_meta.get("opacity", 100)) / 100
                
                image_filter = [
                    f'[{input_idx}:v]scale={width}:{height}'
                ]

                if border_radius > 0:
                    image_filter.append(f'geq=lum=p(X,Y):a=if(lt(pow(min(W/2,X),2)+pow(min(H/2,Y),2),pow({border_radius},2)),255,0)')

                image_filter.append(f'[img{idx}];')
                filter_parts.append(''.join(image_filter))
                
                filter_parts.append(
                    f'[{last_video_temp}][img{idx}]overlay=x={x}:y={y}:'
                    f'enable=\'between(t,{img_meta["startTime"]},{img_meta["endTime"]})\':'
                    f'alpha={opacity}[imgout{idx}];'
                )
                last_video_temp = f'imgout{idx}'

        
        if metadata.get('texts'):
            for idx, text in enumerate(metadata['texts']):
                next_output = f'text{idx}' if idx < len(metadata['texts']) - 1 else 'vout'
                
                x, y = scale_coordinates(text["x"], text["y"], input_width, input_height)
                font_size = int((text["fontSize"] / input_height) * OUTPUT_HEIGHT)
                
                escaped_text = text["description"].replace("'", "\\'")
                opacity = float(text.get("opacity", 100)) / 100
                
                text_filter = (
                    f'[{last_video_temp}]drawtext=text=\'{escaped_text}\':'
                    f'x={x}-tw/2:' # Center horizontally
                    f'y={y}-th/2:' # Center vertically
                    f'fontsize={font_size}:'
                    f'fontcolor={text["color"]}@{opacity}' 
                )
                
                if text.get('backgroundColor'):
                    padding = scale_value(text["padding"], input_height, OUTPUT_HEIGHT)
                    text_filter += f':box=1:boxcolor={text["backgroundColor"]}@{opacity}:boxborderw={padding}'
                
                # Handle font styling
                font_style = 'Arial'
                if text.get('fontWeight') == 'bold':
                    font_style += '-Bold'
                if text.get('isUnderline'):
                    # FFmpeg doesn't support underline directly, so we draw a line under the text
                    text_filter += f':borderw=1:bordercolor={text["color"]}@{opacity}'
                
                text_filter += f':font={font_style}'
                
                text_filter += (
                    f':enable=\'between(t,{text["startTime"]},{text["endTime"]})\''
                    f'[{next_output}];'
                )
                
                filter_parts.append(text_filter)
                last_video_temp = next_output
        else:
            filter_parts.append(f'[{last_video_temp}]null[vout];')
            

        filter_complex = ''.join(filter_parts)

        cmd = [
            'ffmpeg',
            *sum([['-i', path] for path in video_paths], []),
            *sum([['-i', path] for path in image_paths], []),
            '-filter_complex', filter_complex,
            '-map', '[vout]'
        ]
        
        if video_paths:
            cmd.extend(['-map', '[aout]'])
        
        cmd.extend(['-y', output_path])

        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"FFmpeg error output: {result.stderr}")
            raise Exception(f"FFmpeg processing failed: {result.stderr}")

        return send_file(
            output_path,
            mimetype='video/mp4',
            as_attachment=True,
            download_name='final_video.mp4'
        )

    except Exception as e:
        print(f"Error in video processing: {str(e)}")
        return {'error': str(e)}, 500
    
    finally:
        if work_dir and os.path.exists(work_dir):
            try:
                if not os.environ.get('FLASK_DEBUG'):
                    shutil.rmtree(work_dir)
            except Exception as e:
                print(f"Cleanup error: {str(e)}")



if __name__ == '__main__':
    app.run(debug=True, port=8000)