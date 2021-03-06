import time
import colorsys
import math
import socket
import struct
import _thread
import queue
import audioop

"""
TODO:
knowing the bitrate, chop large sample of audio into pieces for visualization, preforming separate fft and playing at correct speed

fade/brightness keeping color ratio
adjust color setting to be more accurate
adjust brightness to be more linear
"""
SCREEN = 1
RPI = 2
WLED = 3

PYAUDIO_IN = 1
UDP_PCM_IN = 2
TEST_WAV = 3

class NeoPixels():
    #init
    def __init__(self, mode=SCREEN, count=300, fade=False, brightness=1.0, ip="wled.me", port=21324):
        self.size = count
        self._MODE = mode
        self.update_pygame = True
        self.lock = _thread.allocate_lock()
        self._fade_thread = None
        self.fadeDelay = 0.5
        self.fadeAmount = 20
        self._socket_queue = queue.SimpleQueue()
        self.port = port
        self.ip = ip

        if fade:
            self.enable_fade()

        if self._MODE == WLED:
            self.pixels = [(0,0,0)] * self.size
            self._wled_thread = _thread.start_new_thread(self._display_wled,())
            self.update_wled = False
        elif self._MODE == RPI:
            import board # must use GPIO 10/12/18/21
            import neopixel
            self.pixels = neopixel.NeoPixel(board.D18, 300, auto_write=False)
        else:
            self.pixels = [(0,0,0)] * self.size
            self._display_thread = _thread.start_new_thread(self._display,())

        self.set_brightness(brightness)

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        if self._MODE == WLED:
            self._wled_thread = None
        elif self._MODE == RPI:
            self.pixels.deinit()
        else:
            self._display_thread = None
        self.stop_fade()

    def __getitem__(self, index):
        with self.lock:
            val = self.pixels[index]
        return val

    def __setitem__(self, index, val):
        with self.lock:
            self.pixels[index] = (int(val[0]),int(val[1]),int(val[2]))

    def __len__(self):
        return len(self.pixels)
        
    #updates the leds with the data in pixels
    def show(self):
        if self._MODE == RPI:
            self.pixels.show()
        elif self._MODE == WLED:
            self.update_wled = True
        else:
            self.update_pygame = True

    #fills pixels with a given color
    def fill(self, color):
        with self.lock:
            if self._MODE == RPI:
                self.pixels.fill(color)
            else:
                for i in range(self.size):
                    self.pixels[i] = color

    def set_brightness(self, amount=1.0):
        if self._MODE == RPI:
            self.pixels.brightness = amount
            self.show()
        else:
            self.show()
        self.brightness = amount

    #starts a thread constantly fading all pixels
    def enable_fade(self, fadeDelay=0.01, fadeAmount=10):
        self.fadeDelay = fadeDelay
        self.fadeAmount = fadeAmount
        if self._fade_thread is None:
            self._fade_thread = _thread.start_new_thread(self._fade, ())

    #stops the fade thread
    def stop_fade(self):
        if self._fade_thread is not None:
            self._fade_thread = None

    #sets the delay of the fade        
    def fade_setup(self, delay=0.05, fadeAmount=20):
        self.fadeDelay = delay
        self.fadeAmount = fadeAmount

    #listens with a socket and gives sound data to the sound_handler
    def run_visualizer_socket(self, sound_handler, args=None, input_mode=PYAUDIO_IN, port=5555, host="127.0.0.1", 
                                                   num_segments=None, f_low=65, f_high=8372, 
                                                   chunk_size=512, N_FFT=2048, raw_fft=False):
        import numpy
        import librosa_mel
        if num_segments is None: #default number of segments/bins in the melspectrum
            num_segments = self.size
        
        if input_mode == TEST_WAV: #play wav file
            self._wav_thread = _thread.start_new_thread(self._wav_handler,())
        elif input_mode == UDP_PCM_IN:
            self._pcm_thread = _thread.start_new_thread(self._pcm_socket_handler,(chunk_size, host, port))
        else:
            self._pyaudio_thread = _thread.start_new_thread(self._pyaudio_handler,(chunk_size,))

        M = librosa_mel.mel(44100, N_FFT, num_segments, fmin=f_low, fmax=f_high)
        while True:
            audio_data = self._socket_queue.get()
            x_fft = numpy.fft.rfft(audio_data, n=N_FFT) # Compute real fast fourier transform
            if raw_fft:
                spectrum = x_fft
            else:
                spectrum = M.dot(abs(x_fft))*100 # Compute mel spectrum and scale a bit

            if args is not None:
                sound_handler(self, spectrum, args)
            else:
                sound_handler(self, spectrum) 

            #time.sleep(chunk_size/44100)#delay while audio is played at 44100hz               

    def _pcm_socket_handler(self, chunk_size=1024, host="127.0.0.1", port=5555):
        import numpy
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((host, port))
            while True:
                data, addr = s.recvfrom(65565)#recive data in chunks (use max size of udp packet for buffer)
                in_data = audioop.tomono(data, 2, 1, 1)#convert to mono stream

                audio_data = numpy.frombuffer(in_data, dtype="<i2") #read as little endian int16
                audio_data = audio_data.astype(numpy.float32)/32767 #make float from -1 to 1
                
                while not self._socket_queue.empty(): #clear the queue
                    self._socket_queue.get_nowait()
                if len(audio_data) == chunk_size:
                    self._socket_queue.put(audio_data)
                    continue
                for i in range(len(audio_data)//chunk_size-1): #divide audio data into chunks and put on the queue
                    self._socket_queue.put(audio_data[i*chunk_size : (i+1)*chunk_size])
                #if len(audio_data)%chunk_size != 0:
                #    self._socket_queue.put(audio_data[len(audio_data)//chunk_size*chunk_size:]) #add smaller chunk at end. may be needed if hickups easy to spot

    def _pyaudio_handler(self, chunk_size=1024):
        import pyaudio
        import numpy
        #init
        p = pyaudio.PyAudio()

        #callback with every frame of audio
        def callback(in_data, frame_count, time_info, status):

            audio_data = numpy.frombuffer(in_data, dtype=numpy.int16)
            audio_data = audio_data.astype(numpy.float32)/32767 #make float from -1 to 1
            self._socket_queue.put(audio_data)
            if self._socket_queue.qsize() > 6: #dont let queue get too big
                self._socket_queue.get_nowait() 
            return (in_data, pyaudio.paContinue)

        try:
            stream = p.open(format=pyaudio.paInt16,
                            channels=1,
                            rate=44100,
                            input=True,   # Do record input.
                            output=False, # Do not play back output.
                            frames_per_buffer=1000,
                            stream_callback=callback)
            while True:
                stream.start_stream()
                while stream.is_active():
                    time.sleep(0.100)
                print("Sound stream is down")
                time.sleep(2)
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

    def _wav_handler(self, filename="bensound.com-energy.wav"):
        import wave
        import pyaudio
        import numpy
        wf = wave.open(filename, 'rb')
        p = pyaudio.PyAudio()

        stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                        channels=wf.getnchannels(),
                        rate=wf.getframerate(),
                        output=True)
        
        # play/send stream
        chunk_size = 4096 #2048 #lower is more latend but higher fps
        data = wf.readframes(chunk_size)
        try:
            while len(data) > 0:
                stream.write(data)
                
                audio_data = numpy.frombuffer(data, dtype=numpy.int16)
                audio_data = audio_data.astype(numpy.float32)/32767 #make float from -1 to 1
                self._socket_queue.put(audio_data)
                if self._socket_queue.qsize() > 6: #dont let queue get too big
                    self._socket_queue.get_nowait() 
            
                data = wf.readframes(chunk_size)
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

    def _fade(self):
        while self._fade_thread is not None:
            time.sleep(self.fadeDelay)
            with self.lock:
                for i in range(self.size):
                    self.pixels[i] = (max(0,self.pixels[i][0]-self.fadeAmount),
                                      max(0,self.pixels[i][1]-self.fadeAmount),
                                      max(0,self.pixels[i][2]-self.fadeAmount))                
            self.show()

    def _display(self):
        import pygame
        import pygame.gfxdraw

        FPS = 90

        pygame.init()
        screen = pygame.display.set_mode((900, 50), pygame.RESIZABLE)
        pygame.display.set_caption("Simulated Neopixels")
        clock = pygame.time.Clock()

        while self._display_thread is not None:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    _thread.interrupt_main()
                    return
                if event.type == pygame.VIDEORESIZE:
                    screen = pygame.display.set_mode((event.w, event.h), pygame.RESIZABLE)

            if self.update_pygame:
                screen.fill((0, 0, 0))
                w, h = screen.get_size()
                for i in range(self.size):
                    pygame.gfxdraw.box(screen,
                                       (w/self.size*i, 0, w/self.size,h),
                                       tuple( map(lambda x: int(x*self.brightness), self.pixels[i]) ))
                self.update_pygame = False

            pygame.display.update() #aka flip
            clock.tick(FPS)
    
    def _display_wled(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        databuf = [None]*(self.size*3+2)
        databuf[0] = 2 #use DRGB data format
        databuf[1] = 2 #wait 2 seconds before resuming WLED default state
        while True:
            if self.update_wled:
                for idx, col in enumerate(self.pixels):
                    databuf[idx*3+2] = int(col[0]*self.brightness)
                    databuf[idx*3+3] = int(col[1]*self.brightness)
                    databuf[idx*3+4] = int(col[2]*self.brightness)
                s.sendto(bytearray(databuf), (self.ip, self.port)) 
                self.update_wled = False


            
            
            
            
            
            

if __name__ == "__main__":
    pixels = NeoPixels(WLED, ip="192.168.1.23")

    def rainbow_pan(speed, numWaves=4):
        offset = 0

        while True:
            time.sleep(0.05)
            offset = (offset+speed/10)%len(pixels)
            for i in range(len(pixels)):
                r, g, b = colorsys.hls_to_rgb((i+offset)*numWaves%len(pixels)/len(pixels), 0.5,1)
                pixels[i] = (int(r*255), int(g*255), int(b*255))
            pixels.show()

    rainbow_pan(20)
   
